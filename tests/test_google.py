"""Google APIs over HTTP: Gmail, Drive, and the Workspace editor reads (Docs/Sheets/Slides).

One file because they are one router (``backlot/routers/google.py``) and one error envelope
(``backlot/errors/google.py``) — Drive and Gmail share ``_gerr`` and the per-family status table, so
splitting them would put two halves of the same contract in two places.
"""

from __future__ import annotations

import base64
import csv
import io
import json
import re
import unicodedata
from urllib.parse import parse_qsl, quote, urlencode

import httpx
import jwt
import pytest
import yaml

from backlot import oauth, sheets_grid, store, synth
from backlot.config import Settings
from backlot.errors import google as gerr
from tests._helpers import (
    client_for,
    crawl_drive,
    crawl_gmail,
    db_count,
    served_id,
    tiny_corpus,
    tok,
)


def test_google_serves_a_corpus_declared_epoch_zero(tmp_path):
    """1970-01-01T00:00:00Z stores as 0, and both Google surfaces must serve that second rather
    than a seeded one. Under truthiness they did not — and for Drive the same expression feeds the
    `q` time filters, so a file answered one date in its body and a different one to a search."""
    s = tiny_corpus(
        tmp_path,
        [
            {
                "source_type": "gmail",
                "doc_id": "gm-zero",
                "title": "S",
                "content": "body",
                "author_email": "a@x.com",
                "visibility": "public",
                "created": 0,
            },
            {
                "source_type": "google_drive",
                "doc_id": "gd-zero",
                "folder": "Reports",
                "title": "Q1",
                "content": "body",
                "author_email": "a@x.com",
                "visibility": "public",
                "created": 0,
            },
        ],
    )
    with client_for(s, reload=True) as c:
        h = {"Authorization": f"Bearer {s.admin_token}"}
        msg = c.get(
            f"/gmail/v1/users/me/messages/{served_id('gmail', 'gm-zero')}", headers=h
        ).json()
        assert msg["internalDate"] == "0"
        f = c.get(
            f"/drive/v3/files/{served_id('google_drive', 'gd-zero')}",
            headers=h,
            params={"fields": "id,createdTime,modifiedTime"},
        ).json()
        assert f["createdTime"].startswith("1970-01-01T00:00:00")
        # the `q` filters read the same second, so a search agrees with the body it returns
        hits = c.get(
            "/drive/v3/files", headers=h, params={"q": "createdTime < '1970-01-02T00:00:00'"}
        ).json()["files"]
        assert served_id("google_drive", "gd-zero") in {x["id"] for x in hits}


# --- admin full-crawl completeness ---------------------------------------------


def test_admin_gmail_crawls_all(client, admin_h, ro_conn):
    assert len(crawl_gmail(client, admin_h)) == db_count(ro_conn, "gmail")


def test_admin_drive_crawls_all(client, admin_h, ro_conn):
    # An unfiltered files.list includes folders on real Drive, and Backlot synthesizes one per
    # container — so a full crawl is every stored file plus every folder.
    folders = ro_conn.execute("SELECT COUNT(*) FROM gdrive_folders").fetchone()[0]
    assert len(crawl_drive(client, admin_h)) == db_count(ro_conn, "google_drive") + folders


# --- content round-trips through each vendor's encoding -------------------------


def _gmail_plain(payload):
    """Extract the text/plain body data from a Gmail payload (top-level or a part, descending into
    the `multipart/alternative` part a message with an attachment nests it in)."""
    if payload.get("body", {}).get("data"):
        return payload["body"]["data"]
    for part in payload.get("parts", []):
        if part["mimeType"] == "text/plain":
            return part["body"]["data"]
        if part["mimeType"].startswith("multipart/"):
            return _gmail_plain(part)
    raise AssertionError("no text/plain part")


# --- Gmail hex message ids --------------------------------------------------------------
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


@pytest.mark.parametrize("mailbox", ["ava", "ava@acme.com"])
def test_a_message_with_no_recipient_serves_no_to_header(tmp_path, mailbox):
    """Real Gmail returns the headers a message has. RFC 5322 allows one with no destination field
    at all -- a Bcc-only send -- so inventing a To makes a case Backlot could never reproduce.
    `Delivered-To` keeps its default: a receiving MTA really does add it.

    `Subject` is the same clause of the same section (3.6 gives `to` and `subject` both 0..1), and
    this format says an empty subject IS the absence of one, so a stated `""` serves no header
    either -- where it used to serve `Subject: ""`.

    The mailbox is stated both ways a corpus states one -- a slug and the owner's address -- because
    the address is what the CALLER is known by, and `users/me/messages` has nothing but that address
    to find the mailbox with."""
    from tests._helpers import build_corpus, client_for, complete, served_id

    settings = build_corpus(
        tmp_path,
        [
            complete(
                "gmail",
                doc_id="gm-no-to",
                mailbox=mailbox,
                title="Bcc-only note",
                content="For the archive.",
                author_email="ava@acme.com",
                created="2026-02-11T08:00:00Z",
            ),
            complete(
                "gmail",
                doc_id="gm-no-subject",
                mailbox=mailbox,
                title="",
                content="No subject on this one.",
                author_email="ava@acme.com",
                created="2026-02-11T09:00:00Z",
            ),
        ],
    )
    tokens = yaml.safe_load(settings.tokens_path.read_text(encoding="utf-8"))
    ava = next(u["token"] for u in tokens["users"] if u["email"] == "ava@acme.com")
    # A second client over a different DB in this module, so the app is re-imported: the
    # lifespan writes its connection onto module-level state (see `client_for`).
    with client_for(settings, reload=True) as c:
        admin = {"authorization": "Bearer admin-service-token"}
        body = c.get(
            f"/gmail/v1/users/me/messages/{served_id('gmail', 'gm-no-to')}", headers=admin
        ).json()
        subjectless = c.get(
            f"/gmail/v1/users/me/messages/{served_id('gmail', 'gm-no-subject')}", headers=admin
        ).json()
        owned = c.get(
            "/gmail/v1/users/me/messages", headers={"authorization": f"Bearer {ava}"}
        ).json()
    headers = {h["name"]: h["value"] for h in body["payload"]["headers"]}
    assert "To" not in headers
    assert headers["Subject"] == "Bcc-only note"
    assert headers["Delivered-To"] == "ava@acme.com"
    assert "Subject" not in {h["name"] for h in subjectless["payload"]["headers"]}
    assert {m["id"] for m in owned["messages"]} == {
        served_id("gmail", "gm-no-to"),
        served_id("gmail", "gm-no-subject"),
    }


def test_gmail_messages_list_serves_hex_ids(client, admin_h):
    """The ids a client receives must look like Gmail's, not like the corpus's dsids: `dsid_…` is
    not hex, so real Gmail would call it an invalid id value.

    Up to 16 digits, not exactly 16: real Gmail renders the integer, so an id whose top nibble is
    zero is shorter there."""
    msgs = client.get(
        "/gmail/v1/users/me/messages", headers=admin_h, params={"maxResults": 10}
    ).json()["messages"]
    assert msgs
    for m in msgs:
        for key in ("id", "threadId"):
            assert 1 <= len(m[key]) <= 16 and not m[key].startswith("0"), m
            assert all(c in "0123456789abcdef" for c in m[key]), m
            assert int(m[key], 16) < 2**63, m
        assert not m["id"].startswith("dsid_")


def test_gmail_hex_id_resolves_to_the_same_document(client, admin_h, ro_conn):
    """The hex id maps back to its dsid, so the body a client reads by hex is the stored body. A
    one-way id would make every message unreadable."""

    row = _a_gmail_row(ro_conn)
    hexid = row["id"]
    m = client.get(
        f"/gmail/v1/users/me/messages/{hexid}", headers=admin_h, params={"format": "full"}
    ).json()
    assert m["id"] == hexid
    assert base64.urlsafe_b64decode(_gmail_plain(m["payload"])).decode() == row["content"]
    # The stored column is lowercase hex, but a client may spell the id in either case (Gmail's ids
    # are case-insensitive hex) -- resolution must fold case rather than requiring the exact stored
    # spelling. `store.gmail_id_spelling` is the one place that has to do this.
    upper = client.get(
        f"/gmail/v1/users/me/messages/{hexid.upper()}", headers=admin_h, params={"format": "full"}
    ).json()
    assert upper["id"] == m["id"]


@pytest.mark.parametrize(
    "spelling, same_as",
    [
        ("{ROOT}", "{root}"),
        ("0{root}", "{root}"),
        ("00{root}", "{root}"),
        ("0000000000{root}", "{root}"),
        ("0{ROOT}", "{root}"),
        ("0{reply}", "1"),
    ],
)
def test_gmail_threads_get_reads_an_id_as_a_hex_integer(
    client, admin_h, ro_conn, spelling, same_as
):
    """`threads.get` on a thread's id in uppercase, or with one, two or ten zeros in front and its
    hex in either case, serves the thread as the lowercase id without them does; on a reply's id
    with a zero in front it serves the 404 of an id the mailbox does not hold (`1`). The measurement
    is beside the return in `gmail_thread_get`."""
    row = ro_conn.execute(
        "SELECT * FROM gmail_messages WHERE COALESCE(thread_id,'') != '' "
        "AND thread_id != id LIMIT 1"
    ).fetchone()
    assert row is not None, "SAMPLE should hold a threaded reply"
    ids = {"root": row["thread_id"], "ROOT": row["thread_id"].upper(), "reply": row["id"]}

    def threads_get(thread_id):
        return client.get(
            f"/gmail/v1/users/me/threads/{thread_id.format(**ids)}",
            headers=admin_h,
            params={"format": "minimal"},
        )

    got, want = threads_get(spelling), threads_get(same_as)
    assert (got.status_code, got.json()) == (want.status_code, want.json())


def test_gmail_thread_id_matches_the_message_id_for_a_lone_message(client, admin_h, ro_conn):
    """Threads share the message id space in real Gmail, so a message that is its own thread root
    reports the same value twice — and `threads.get` resolves it."""

    row = ro_conn.execute(
        "SELECT * FROM gmail_messages WHERE COALESCE(thread_id, '') = '' LIMIT 1"
    ).fetchone()
    if row is None:
        row = ro_conn.execute(
            "SELECT * FROM gmail_messages WHERE thread_id = id LIMIT 1"
        ).fetchone()
    assert row is not None, "SAMPLE should hold a message that is its own thread"
    hexid = row["id"]
    m = client.get(f"/gmail/v1/users/me/messages/{hexid}", headers=admin_h).json()
    assert m["id"] == m["threadId"] == hexid
    t = client.get(f"/gmail/v1/users/me/threads/{hexid}", headers=admin_h)
    assert t.status_code == 200 and t.json()["id"] == hexid
    upper = client.get(f"/gmail/v1/users/me/threads/{hexid.upper()}", headers=admin_h)
    assert upper.status_code == 200 and upper.json() == t.json()


def test_gmail_a_reply_is_served_under_its_roots_thread_id_only(client, admin_h, ro_conn):
    """A reply reports its root's id as `threadId`, `threads.get` on that id serves the thread with
    the reply in it, and `threads.get` on the reply's own id gets the answer a well-formed id the
    mailbox does not hold gets. The measurement is beside the fallback in `gmail_thread_get`.
    """
    row = ro_conn.execute(
        "SELECT * FROM gmail_messages WHERE COALESCE(thread_id,'') != '' "
        "AND thread_id != id LIMIT 1"
    ).fetchone()
    assert row is not None, "SAMPLE should hold a threaded reply"
    m = client.get(f"/gmail/v1/users/me/messages/{row['id']}", headers=admin_h).json()
    # `thread_id` holds the ROOT'S OWN served id — no re-derivation on either side.
    assert m["threadId"] == row["thread_id"]
    assert m["id"] != m["threadId"]
    thread = client.get(f"/gmail/v1/users/me/threads/{m['threadId']}", headers=admin_h)
    assert thread.status_code == 200
    assert row["id"] in [x["id"] for x in thread.json()["messages"]]
    reply = client.get(f"/gmail/v1/users/me/threads/{row['id']}", headers=admin_h)
    unknown = client.get("/gmail/v1/users/me/threads/1", headers=admin_h)
    assert reply.status_code == unknown.status_code == 404
    assert reply.json() == unknown.json()


def test_gmail_threads_list_is_the_mailbox_searched_or_not(client, tokens):
    """One listing, one scope. `threads.list` counted the threads the caller had WRITTEN in, while
    `threads.list?q=` filtered the caller's MAILBOX — and passed the caller's address where a
    mailbox name goes, so a search of one's own mail came back empty."""
    h = {"Authorization": f"Bearer {tokens['ava@acme.com']}"}
    plain = client.get("/gmail/v1/users/me/threads", headers=h).json()
    searched = client.get("/gmail/v1/users/me/threads", headers=h, params={"q": "gateway"}).json()
    thread = served_id("gmail", "gm-thread-root")
    assert thread in [t["id"] for t in plain["threads"]]
    assert [t["id"] for t in searched["threads"]] == [thread]
    # A reply is not a thread of its own, and a thread several of whose messages match is listed once.
    assert served_id("gmail", "gm-thread-reply") not in [t["id"] for t in plain["threads"]]
    assert plain["resultSizeEstimate"] == len(plain["threads"])
    # Two messages, one thread — so the two totals are two numbers, in the profile and on the label.
    profile = client.get("/gmail/v1/users/me/profile", headers=h).json()
    inbox = client.get("/gmail/v1/users/me/labels/INBOX", headers=h).json()
    assert profile["messagesTotal"] > profile["threadsTotal"] == len(plain["threads"])
    assert (inbox["messagesTotal"], inbox["threadsTotal"]) == (
        profile["messagesTotal"],
        profile["threadsTotal"],
    )


@pytest.mark.parametrize(
    "msg_key",
    ["owner", "other", "missing", "non_hex"],
)
@pytest.mark.parametrize(
    "att_key, expect_status",
    [("valid", 200), ("bogus", 400), ("altered", 400)],
)
def test_gmail_attachment_errors(client, admin_h, ro_conn, msg_key, att_key, expect_status):
    """Under each of the four message ids, an attachment id gets the answer it gets under its own
    message: 200 with its bytes when a message holds it, 400 when none does, as the comment in
    `gmail_attachment` records."""
    row = ro_conn.execute(
        "SELECT * FROM gmail_messages WHERE COALESCE(attachments,'') NOT IN ('', '[]') LIMIT 1"
    ).fetchone()
    assert row is not None, "SAMPLE should hold a message with an attachment"
    hexid = row["id"]
    other_row = ro_conn.execute(
        "SELECT id FROM gmail_messages WHERE id != ? LIMIT 1", (hexid,)
    ).fetchone()
    assert other_row is not None, "SAMPLE should hold a second gmail message"
    m = client.get(
        f"/gmail/v1/users/me/messages/{hexid}", headers=admin_h, params={"format": "full"}
    ).json()
    valid_att = next(p for p in m["payload"]["parts"] if p.get("filename"))["body"]["attachmentId"]

    msg_id = {
        "owner": hexid,
        "other": other_row["id"],
        "missing": "0000000000000001",
        "non_hex": "zzz",
    }[msg_key]
    att_id = (
        valid_att
        if att_key == "valid"
        else "bogus"
        if att_key == "bogus"
        else valid_att[:-4] + "abcd"
    )

    r = client.get(
        f"/gmail/v1/users/me/messages/{msg_id}/attachments/{att_id}",
        headers=admin_h,
    )

    assert r.status_code == expect_status
    if expect_status == 400:
        assert r.json() == {
            "error": {
                "code": 400,
                "message": "Invalid attachment token",
                "errors": [
                    {
                        "message": "Invalid attachment token",
                        "domain": "global",
                        "reason": "invalidArgument",
                    }
                ],
                "status": "INVALID_ARGUMENT",
            }
        }
    else:
        owned = client.get(
            f"/gmail/v1/users/me/messages/{hexid}/attachments/{valid_att}", headers=admin_h
        ).json()
        assert owned["size"] > 0
        assert r.json() == owned


_ATTACHMENT_ACL = [
    {
        "source_type": "gmail",
        "doc_id": "ava-deck",
        "mailbox": "ava",
        "title": "Deck",
        "content": "Deck attached.",
        "author_email": "ava@acme.com",
        "readers": ["ava@acme.com"],
        "created": "2026-02-01T09:00:00Z",
        "attachments": [{"filename": "deck.pdf", "mime": "application/pdf", "content": "deck"}],
    },
    {
        "source_type": "gmail",
        "doc_id": "ava-memo",
        "mailbox": "ava",
        "title": "Memo",
        "content": "Memo attached.",
        "author_email": "ava@acme.com",
        "readers": ["ava@acme.com"],
        "created": "2026-02-02T09:00:00Z",
        "attachments": [{"filename": "memo.txt", "mime": "text/plain"}],
    },
    {
        "source_type": "gmail",
        "doc_id": "mia-note",
        "mailbox": "mia",
        "title": "Note",
        "content": "No attachment.",
        "author_email": "mia@acme.com",
        "readers": ["mia@acme.com"],
        "created": "2026-02-03T09:00:00Z",
    },
]


@pytest.fixture
def attachment_acl(tmp_path):
    """Two of ava's messages with one attachment each, and a message of mia's with none. Yields the
    client, a header per caller, and each attachment id by the doc it belongs to."""
    settings = tiny_corpus(tmp_path, _ATTACHMENT_ACL)
    tokens = yaml.safe_load(settings.tokens_path.read_text(encoding="utf-8"))
    h = {"admin": {"Authorization": f"Bearer {settings.admin_token}"}}
    for name in ("ava", "mia"):
        h[name] = {"Authorization": f"Bearer {tok(tokens, f'{name}@acme.com')}"}
    with client_for(settings, reload=True) as client:
        att = {}
        for doc in ("ava-deck", "ava-memo"):
            m = client.get(
                f"/gmail/v1/users/me/messages/{served_id('gmail', doc)}", headers=h["admin"]
            ).json()
            att[doc] = next(p for p in m["payload"]["parts"] if p.get("filename"))["body"][
                "attachmentId"
            ]
        yield client, h, att


@pytest.mark.parametrize("under", ["ava-deck", "ava-memo", "mia-note"])
@pytest.mark.parametrize("caller", ["admin", "ava", "mia"])
def test_gmail_attachment_is_found_by_its_id_within_the_acl(
    attachment_acl, monkeypatch, under, caller
):
    """Under each of the three message ids, each of ava's attachment ids lands its own bytes for
    the admin and ava, and mia, who cannot see ava's messages, gets the 400 an id nothing has gets.
    The memo states no `content`, so its bytes are the stand-in `_att_content` writes, which names
    the attachment's id. The scan the comment in `gmail_attachment` describes is skipped only when
    the path names the message holding the attachment and the caller can see it. It reads ava's two
    messages for the admin and ava, and none for mia, whose one message holds no attachment."""
    client, h, att = attachment_acl
    scanned, scan = [], store.gmail_rows_with_attachments

    def spy(*args, **kwargs):
        rows = scan(*args, **kwargs)
        scanned.append({row["id"] for row in rows})
        return rows

    monkeypatch.setattr(store, "gmail_rows_with_attachments", spy)
    want = {"ava-deck": b"deck", "ava-memo": f"attachment {att['ava-memo']}".encode()}
    reads = set() if caller == "mia" else {served_id("gmail", doc) for doc in want}
    for doc, content in want.items():
        scanned.clear()
        r = client.get(
            f"/gmail/v1/users/me/messages/{served_id('gmail', under)}/attachments/{att[doc]}",
            headers=h[caller],
        )
        if caller == "mia":
            assert r.status_code == 400, doc
            assert r.json()["error"]["message"] == "Invalid attachment token", doc
        else:
            assert r.status_code == 200, doc
            assert base64.urlsafe_b64decode(r.json()["data"]) == content, doc
        assert scanned == ([] if under == doc and caller != "mia" else [reads]), doc


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
    """An id that is not a parsable in-range hex integer is 400 INVALID_ARGUMENT "Invalid id
    value", not 404. The last row is a `dsid_…`, which real would refuse the same way."""
    for kind in ("messages", "threads"):
        r = client.get(f"/gmail/v1/users/me/{kind}/{mid}", headers=admin_h)
        assert r.status_code == 400, f"{kind}/{mid}: {r.status_code}"
        e = r.json()["error"]
        assert e["message"] == "Invalid id value"
        assert e["status"] == "INVALID_ARGUMENT"
        assert e["errors"][0]["reason"] == "invalidArgument"


def test_gmail_hex_ids_still_enforce_the_acl(client, admin_h, tokens_yaml, ro_conn):
    """Resolving a served id must not become a way around the ACL. `id` is the primary key, not a
    per-caller view — it names the SAME row regardless of who asks — so `visible_ids` has to be
    part of the query that reads it (`store.gmail_by_id`'s ACL-scoped lookup), not a
    separate check applied only after an unscoped resolve. The CFO's comp review is granted to cfo
    alone."""

    row = ro_conn.execute(
        "SELECT * FROM gmail_messages WHERE title LIKE 'Confidential comp%'"
    ).fetchone()
    hexid = row["id"]
    assert client.get(f"/gmail/v1/users/me/messages/{hexid}", headers=admin_h).status_code == 200
    cfo = {"Authorization": f"Bearer {tok(tokens_yaml, 'cfo@acme.com')}"}
    assert client.get(f"/gmail/v1/users/me/messages/{hexid}", headers=cfo).status_code == 200
    outsider = {"Authorization": f"Bearer {tok(tokens_yaml, 'mia@acme.com')}"}
    r = client.get(f"/gmail/v1/users/me/messages/{hexid}", headers=outsider)
    assert r.status_code == 404
    assert r.json()["error"]["message"] == "Requested entity was not found."


def test_gmail_body_roundtrip(client, admin_h, ro_conn):

    doc = ro_conn.execute("SELECT * FROM gmail_messages LIMIT 1").fetchone()
    m = client.get(
        f"/gmail/v1/users/me/messages/{doc['id']}",
        headers=admin_h,
        params={"format": "full"},
    ).json()
    body = base64.urlsafe_b64decode(_gmail_plain(m["payload"])).decode()
    assert body == doc["content"]
    subj = next(h["value"] for h in m["payload"]["headers"] if h["name"] == "Subject")
    assert subj == doc["title"]


def test_gmail_messages_list_ordered_by_internaldate_desc(client, admin_h, ro_conn):
    # Real Gmail returns messages.list newest-first by internalDate. Listing by id instead is hash
    # order, which makes a capped "newest N" effectively random by date.
    listed = client.get(
        "/gmail/v1/users/me/messages", headers=admin_h, params={"maxResults": 50}
    ).json()["messages"]
    got = [m["id"] for m in listed]
    # the stable total order the endpoint must produce: created_ts DESC, id ASC as tie-break
    # the served ids are hex, so the expectation is the hex of that stable order

    expected = [
        r["id"]
        for r in ro_conn.execute(
            "SELECT id FROM gmail_messages ORDER BY created_ts DESC, id LIMIT 50"
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


def test_gmail_max_results_is_capped_at_500(tmp_path, monkeypatch):
    """The cap `_gmail_page` records, on both listings."""
    from backlot.routers import google
    from tests._helpers import corpus_client

    records = [
        {
            "source_type": "gmail",
            "doc_id": f"m{i}",
            "mailbox": "ava",
            "title": f"Message {i}",
            "content": f"Body {i}.",
            "author_email": "bob@acme.com",
            "readers": ["ava@acme.com"],
            "created": f"2026-01-{i % 28 + 1:02d}T{i // 28 % 24:02d}:00:00Z",
        }
        for i in range(502)
    ]
    with corpus_client(tmp_path, records) as (client, settings):
        h = {"Authorization": f"Bearer {settings.admin_token}"}
        for kind in ("messages", "threads"):
            for asked, served in (
                (499, 499),
                (500, 500),
                (501, 500),
                (1000, 500),
                (100000, 500),
                (2147483647, 500),
            ):
                page = client.get(
                    f"/gmail/v1/users/me/{kind}", headers=h, params={"maxResults": asked}
                ).json()
                assert len(page[kind]) == served, (kind, asked)
                assert "nextPageToken" in page, (kind, asked)
        # BACKLOT_MAX_PAGE_SIZE still caps a sent value: 10 serves 5.
        monkeypatch.setattr(google.get_settings(), "max_page_size", 5)
        for kind in ("messages", "threads"):
            page = client.get(
                f"/gmail/v1/users/me/{kind}", headers=h, params={"maxResults": 10}
            ).json()
            assert len(page[kind]) == 5, kind
            assert "nextPageToken" in page, kind


# The rows `_gmail_page` records. A `size` is served, `uint32` is the proto layer's refusal
# of each named repeat, and `maxResults` is `Invalid maxResults`. The measurement is that
# function's.
_GMAIL_MAX_RESULTS = [
    (["+2"], "size", 2),
    (["02"], "size", 2),
    (["0", "3"], "size", 3),
    (["-0"], "uint32", ("-0",)),
    (["4294967296"], "uint32", ("4294967296",)),
    (["abc", "3"], "uint32", ("abc",)),
    (["abc", "def"], "uint32", ("abc", "def")),
    (["-1"], "uint32", ("-1",)),
    (["abc"], "uint32", ("abc",)),
    ([""], "uint32", ("",)),
    (["1.5"], "uint32", ("1.5",)),
    (["\u0663"], "uint32", ("\u0663",)),
    (["-0", "3"], "uint32", ("-0",)),
    (["0"], "maxResults", None),
    (["+0"], "maxResults", None),
    (["00"], "maxResults", None),
    (["2147483648"], "maxResults", None),
    (["+2147483648"], "maxResults", None),
    (["4294967295"], "maxResults", None),
    (["3", "0"], "maxResults", None),
    (["3", "2147483648"], "maxResults", None),
]


@pytest.mark.parametrize("kind", ["messages", "threads"])
@pytest.mark.parametrize("values, shape, named", _GMAIL_MAX_RESULTS)
def test_gmail_refuses_a_max_results_it_cannot_read(client, admin_h, kind, values, shape, named):
    """The refusals `_gmail_page` records, on both listings."""
    r = client.get(
        f"/gmail/v1/users/me/{kind}",
        headers=admin_h,
        params=[("maxResults", value) for value in values],
    )
    if shape == "size":
        assert r.status_code == 200, (kind, values, r.text)
        body = r.json()
        assert body["resultSizeEstimate"] >= named, (kind, values)
        assert len(body[kind]) == named, (kind, values)
        return
    assert r.status_code == 400, (kind, values, r.text)
    err = r.json()["error"]
    if shape == "maxResults":
        assert err == {
            "code": 400,
            "message": "Invalid maxResults",
            "errors": [
                {
                    "message": "Invalid maxResults",
                    "domain": "global",
                    "reason": "invalidArgument",
                }
            ],
            "status": "INVALID_ARGUMENT",
        }, (kind, values)
        return
    messages = [f"Invalid value at 'max_results' (TYPE_UINT32), \"{raw}\"" for raw in named]
    message = "\n".join(messages)
    assert err == {
        "code": 400,
        "message": message,
        "errors": [{"message": message, "reason": "invalid"}],
        "status": "INVALID_ARGUMENT",
        "details": [
            {
                "@type": "type.googleapis.com/google.rpc.BadRequest",
                "fieldViolations": [
                    {"field": "max_results", "description": one} for one in messages
                ],
            }
        ],
    }, (kind, values)


_GMAIL_TOKEN = {
    "code": 400,
    "message": "Invalid pageToken",
    "errors": [{"message": "Invalid pageToken", "domain": "global", "reason": "invalidArgument"}],
    "status": "INVALID_ARGUMENT",
}
_GMAIL_RANGE = {
    "code": 400,
    "message": "Invalid maxResults",
    "errors": [{"message": "Invalid maxResults", "domain": "global", "reason": "invalidArgument"}],
    "status": "INVALID_ARGUMENT",
}
_GMAIL_UINT32_ABC = {
    "code": 400,
    "message": "Invalid value at 'max_results' (TYPE_UINT32), \"abc\"",
    "errors": [
        {"message": "Invalid value at 'max_results' (TYPE_UINT32), \"abc\"", "reason": "invalid"}
    ],
    "status": "INVALID_ARGUMENT",
    "details": [
        {
            "@type": "type.googleapis.com/google.rpc.BadRequest",
            "fieldViolations": [
                {
                    "field": "max_results",
                    "description": "Invalid value at 'max_results' (TYPE_UINT32), \"abc\"",
                }
            ],
        }
    ],
}


# fmt: off
_GMAIL_PAGE_TOKEN_ROWS = [
    ([("pageToken", "bogus")], _GMAIL_TOKEN, _GMAIL_TOKEN),
    ([("pageToken", "")], None, None),
    ([("maxResults", "1"), ("pageToken", "{token}")], "second", "second"),
    ([("maxResults", "0"), ("pageToken", "bogus")], _GMAIL_TOKEN, _GMAIL_RANGE),
    ([("pageToken", "garbage"), ("maxResults", "0")], _GMAIL_TOKEN, _GMAIL_RANGE),
    ([("maxResults", "2147483648"), ("pageToken", "garbage")], _GMAIL_TOKEN, _GMAIL_RANGE),
    ([("pageToken", "garbage"), ("maxResults", "abc")], _GMAIL_UINT32_ABC, _GMAIL_UINT32_ABC),
    ([("pageToken", "garbage"), ("pageToken", "")], None, None),
    ([("pageToken", ""), ("pageToken", "garbage")], _GMAIL_TOKEN, _GMAIL_TOKEN),
    ([("maxResults", "1"), ("pageToken", "garbage"), ("pageToken", "{token}")], "second", "second"),
    ([("maxResults", "1"), ("pageToken", "{token}"), ("pageToken", "garbage")], _GMAIL_TOKEN, _GMAIL_TOKEN),
]
# fmt: on


@pytest.mark.parametrize("query, on_messages, on_threads", _GMAIL_PAGE_TOKEN_ROWS)
@pytest.mark.parametrize("kind", ["messages", "threads"])
def test_gmail_lists_read_a_page_token_as_real_does(
    client, admin_h, kind, query, on_messages, on_threads
):
    """The rules `_gmail_page` and `gerr.invalid_page_token` record, one request per row and
    listing. `None` is the answer the request gets without a `pageToken`, `"second"` the page after
    the first at `maxResults=1`, and `{token}` the `nextPageToken` of that first page."""
    path = f"/gmail/v1/users/me/{kind}"
    first = client.get(path, headers=admin_h, params={"maxResults": 1}).json()
    query = [(k, v.format(token=first["nextPageToken"])) for k, v in query]
    r = client.get(path, headers=admin_h, params=query)
    want = on_messages if kind == "messages" else on_threads
    if want is None:
        without = [(k, v) for k, v in query if k != "pageToken"]
        assert r.content == client.get(path, headers=admin_h, params=without).content
    elif want == "second":
        both = client.get(path, headers=admin_h, params={"maxResults": 2}).json()[kind]
        assert r.status_code == 200, r.text
        assert r.json()[kind] == both[1:]
    else:
        assert (r.status_code, _gerr(r)) == (400, want)


def test_gmail_attachment_size_matches_part_metadata(client, admin_h, ro_conn):
    # Real Gmail's contract: a part's body.size equals the byte length attachments.get serves, so a
    # client can stat an attachment from message metadata alone. Reporting the corpus-declared
    # `size` (e.g. 2048) while attachments.get reports `_byte_len(content)` breaks that.
    row = ro_conn.execute(
        "SELECT id FROM gmail_messages WHERE attachments IS NOT NULL "
        "AND attachments != '[]' LIMIT 1"
    ).fetchone()
    if row is None:
        pytest.skip("no gmail message with an attachment in this subset")

    hexid = row["id"]
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
    # The row's own `id`, not the corpus's identifier: hitting the route by the corpus id would
    # stop exercising the id resolution path without the test noticing.
    text = client.get(
        f"/drive/v3/files/{doc['id']}/export",
        headers=admin_h,
        params={"mimeType": "text/plain"},
    ).text
    assert doc["content"] in text and text.startswith(doc["title"])


def test_drive_in_owners_query(client, admin_h, ro_conn):
    # real Drive supports `'<owner>' in owners`; Backlot must filter by owner (email or name),
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
    # google-api-python-client posts a multipart/mixed batch to /batch; Backlot must dispatch each
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
    # dropped query defaults to full, so if Backlot ignored it, `format=minimal` would still carry a
    # payload. A batch-trusting client that caches these would cache bodyless messages otherwise.
    mid = client.get(
        "/gmail/v1/users/me/messages", headers=admin_h, params={"maxResults": 1}
    ).json()["messages"][0]["id"]
    assert "payload" in _batch_one(client, admin_h, mid, "full", uri)  # format=full honored
    assert "payload" not in _batch_one(
        client, admin_h, mid, "minimal", uri
    )  # format=minimal honored


_REDIRECTED = {
    "error": {
        "code": 302,
        "message": "Unknown Error.",
        "errors": [{"message": "Unknown Error.", "domain": "global", "reason": "backendError"}],
        "status": "UNKNOWN",
    }
}
_UNIMPLEMENTED_MESSAGE = "Operation is not implemented, or supported, or enabled."
_UNIMPLEMENTED = {
    "error": {"code": 501, "message": _UNIMPLEMENTED_MESSAGE, "status": "UNIMPLEMENTED"}
}
_UNIMPLEMENTED_AT_XGAFV_1 = {
    "error": {
        **_UNIMPLEMENTED["error"],
        "errors": [
            {"message": _UNIMPLEMENTED_MESSAGE, "domain": "global", "reason": "notImplemented"}
        ],
    }
}
_DRIVE_BATCH = "/batch/drive/v3?quotaUser=7"
_SHEETS_BATCH = "/batch?quotaUser=7"
_BAD = "Bearer nope"
_MIA = "{mia}"  # the scoped token, which cannot see the spreadsheet
_ANON = "anonymous"  # no credential on the part or on the batch
_NORMALISED = "/batch/drive/v3?quotaUser=7&foo=%41&b%61r=1&~t=%7e&&"
_A1_FILTER = '{"dataFilters": [{"a1Range": "A1"}]}'
_BAD_FILTER = '{"dataFilters": "abc"}'
_CHALLENGE = 'Bearer realm="https://accounts.google.com/"'  # a credential that names no token
_CHALLENGED = f'{_CHALLENGE}, error="invalid_token"'  # one that names a token that does not resolve

# One part per batch, as real's Drive batch and Sheets batch answered it. A row is the batch URI
# with its query, the part as (method, target, body, its own Authorization), the status in the batch
# and the status of the same request sent on its own, then a 302's `Location` below the server's
# base URL, a 401's `WWW-Authenticate`, the message of a 400 a token in the query answers, or a
# 501's body. The status on its own is ``None`` where real's is one Backlot does not give: the
# export with an empty `alt=` (400), the id holding a `%` no two hex digits follow (503), the two
# downloads with `callback=a%20b` (503), the export with that `callback` beside `$.xgafv=9` (400)
# and the media read with `access_token=nope` (Google's automated-queries page). It is ``None``
# too on every row whose batch URI names a token, which the request on its own does not carry. `{t}`
# in a URI or a part's target is the admin token. `_drive_batch_download`, `_drive_batch_redirect`,
# `_query_credential` and `_workbook` record the rules.
# fmt: off
_BATCH_ROWS = [
    # a Drive download is redirected ahead of the lookup and the typed, `fields` and `mimeType`
    # refusals
    (_DRIVE_BATCH, ("GET", "/drive/v3/files/{sheet}/export?mimeType=text/csv", None, None), 302, 200, "download/drive/v3/files/{sheet}/export?mimeType=text/csv&quotaUser=7"),
    (_DRIVE_BATCH, ("GET", "/drive/v3/files/{doc}/export?mimeType=", None, None), 302, 400, "download/drive/v3/files/{doc}/export?mimeType=&quotaUser=7"),
    (_DRIVE_BATCH, ("GET", "/drive/v3/files/{doc}/export", None, None), 302, 400, "download/drive/v3/files/{doc}/export?quotaUser=7"),
    ("/batch/drive/v3", ("GET", "/drive/v3/files/{doc}/export", None, None), 302, 400, "download/drive/v3/files/{doc}/export"),
    (_DRIVE_BATCH, ("GET", "/drive/v3/files/{nope}/export?mimeType=text/plain", None, None), 302, 404, "download/drive/v3/files/{nope}/export?mimeType=text/plain&quotaUser=7"),
    (_DRIVE_BATCH, ("GET", "/drive/v3/files/{sheet}/export?mimeType=text/csv", None, _MIA), 302, 404, "download/drive/v3/files/{sheet}/export?mimeType=text/csv&quotaUser=7"),
    (_DRIVE_BATCH, ("GET", "/drive/v3/files/{doc}/export?mimeType=text/plain&alt=media", None, None), 302, 200, "download/drive/v3/files/{doc}/export?mimeType=text/plain&alt=media&quotaUser=7"),
    (_DRIVE_BATCH, ("GET", "/drive/v3/files/{doc}/export?mimeType=text/plain&alt=", None, None), 302, None, "download/drive/v3/files/{doc}/export?mimeType=text/plain&alt=&quotaUser=7"),
    (_DRIVE_BATCH, ("GET", "/drive/v3/files/{pdf}?alt=media", None, None), 302, 200, "download/drive/v3/files/{pdf}?alt=media&quotaUser=7"),
    # a trailing slash is routed past but kept in the `Location`
    (_DRIVE_BATCH, ("GET", "/drive/v3/files/{pdf}//?alt=media", None, None), 302, 200, "download/drive/v3/files/{pdf}//?alt=media&quotaUser=7"),
    (_DRIVE_BATCH, ("GET", "/drive/v3/files/{doc}/export/?mimeType=text/plain", None, None), 302, 200, "download/drive/v3/files/{doc}/export/?mimeType=text/plain&quotaUser=7"),
    (_DRIVE_BATCH, ("GET", "/drive/v3/files/{pdf}?alt=MEDIA", None, None), 302, 200, "download/drive/v3/files/{pdf}?alt=MEDIA&quotaUser=7"),
    (_DRIVE_BATCH, ("GET", "/drive/v3/files/{pdf}?alt=media&alt=json", None, None), 302, 200, "download/drive/v3/files/{pdf}?alt=media&alt=json&quotaUser=7"),
    (_DRIVE_BATCH, ("GET", "/drive/v3/files/{nope}?alt=media", None, None), 302, 404, "download/drive/v3/files/{nope}?alt=media&quotaUser=7"),
    (_DRIVE_BATCH, ("GET", "/drive/v3/files/{doc}?alt=media", None, None), 302, 403, "download/drive/v3/files/{doc}?alt=media&quotaUser=7"),
    (_DRIVE_BATCH, ("GET", "/drive/v3/files/{pdf}?alt=media&supportsAllDrives=NOPE", None, None), 302, 400, "download/drive/v3/files/{pdf}?alt=media&supportsAllDrives=NOPE&quotaUser=7"),
    (_DRIVE_BATCH, ("GET", "/drive/v3/files/{pdf}?alt=media&fields=bogus", None, None), 302, 200, "download/drive/v3/files/{pdf}?alt=media&fields=bogus&quotaUser=7"),
    (_DRIVE_BATCH, ("GET", "/drive/v3/files/{pdf}?alt=media&acknowledgeAbuse=true", None, None), 302, 200, "download/drive/v3/files/{pdf}?alt=media&acknowledgeAbuse=true&quotaUser=7"),
    # the batch's query follows the part's, less each name the part's query carries
    ("/batch/drive/v3?quotaUser=7&prettyPrint=false", ("GET", "/drive/v3/files/{doc}/export?mimeType=text/plain&prettyPrint=true", None, None), 302, 200, "download/drive/v3/files/{doc}/export?mimeType=text/plain&prettyPrint=true&quotaUser=7"),
    (_DRIVE_BATCH, ("GET", "/drive/v3/files/{pdf}?alt=media&quotaUser=PARTQ", None, None), 302, 200, "download/drive/v3/files/{pdf}?alt=media&quotaUser=PARTQ"),
    ("/batch/drive/v3?quotaUser=7&foo=1&foo=2", ("GET", "/drive/v3/files/{doc}/export?mimeType=text/plain&Foo=3", None, None), 302, 200, "download/drive/v3/files/{doc}/export?mimeType=text/plain&Foo=3&quotaUser=7&foo=1&foo=2"),
    ("/batch/drive/v3?quotaUser=7&foo=1&foo=2", ("GET", "/drive/v3/files/{doc}/export?mimeType=text/plain&foo=", None, None), 302, 200, "download/drive/v3/files/{doc}/export?mimeType=text/plain&foo=&quotaUser=7"),
    ("/batch/drive/v3?quotaUser=7&a%20b=c%2Fd&e=f+g&h", ("GET", "/drive/v3/files/{doc}/export?mimeType=text/plain", None, None), 302, 200, "download/drive/v3/files/{doc}/export?mimeType=text/plain&quotaUser=7&a%20b=c%2Fd&e=f+g&h="),
    # its pairs as real writes them: an empty one dropped, an escaped letter or digit decoded, any
    # other escape's hex in upper case, a bare name given `=`, and names matched as written
    (_NORMALISED, ("GET", "/drive/v3/files/{doc}/export?mimeType=text/plain&&y=%41", None, None), 302, 200, "download/drive/v3/files/{doc}/export?mimeType=text/plain&y=A&quotaUser=7&foo=A&bar=1&~t=%7E"),
    (_NORMALISED, ("GET", "/drive/v3/files/{doc}/export?mimeType=text/plain&fo%6F=3", None, None), 302, 200, "download/drive/v3/files/{doc}/export?mimeType=text/plain&foo=3&quotaUser=7&bar=1&~t=%7E"),
    (_NORMALISED, ("GET", "/drive/v3/files/{doc}/export?mimeType=text/plain&x%2Dy=1&x%2fy=2", None, None), 302, 200, "download/drive/v3/files/{doc}/export?mimeType=text/plain&x%2Dy=1&x%2Fy=2&quotaUser=7&foo=A&bar=1&~t=%7E"),
    (_NORMALISED, ("GET", "/drive/v3/files/{doc}/export?mimeType=text/plain&n=%7E&k=%4a&z", None, None), 302, 200, "download/drive/v3/files/{doc}/export?mimeType=text/plain&n=%7E&k=J&z=&quotaUser=7&foo=A&bar=1&~t=%7E"),
    (_NORMALISED, ("GET", "/drive/v3/files/{doc}/export?mimeType=text/plain&~t=1", None, None), 302, 200, "download/drive/v3/files/{doc}/export?mimeType=text/plain&~t=1&quotaUser=7&foo=A&bar=1"),
    ("/batch/drive/v3?quotaUser=7&n%31=%32", ("GET", "/drive/v3/files/{doc}/export?mimeType=text/plain&d=%31&dot=%2E&us=%5F&pct=%25&q=%3f", None, None), 302, 200, "download/drive/v3/files/{doc}/export?mimeType=text/plain&d=1&dot=%2E&us=%5F&pct=%25&q=%3F&quotaUser=7&n1=2"),
    ("/batch/drive/v3?quotaUser=7&n%31=%32", ("GET", "/drive/v3/files/{doc}/export?mimeType=text/plain&n1=9", None, None), 302, 200, "download/drive/v3/files/{doc}/export?mimeType=text/plain&n1=9&quotaUser=7"),
    # its path's escapes, as `_drive_batch_redirect` records them
    (_DRIVE_BATCH, ("GET", "/drive/v3/files/a%20b%3fc%25d?alt=media", None, None), 302, 404, "download/drive/v3/files/a%20b%3Fc%25d?alt=media&quotaUser=7"),
    (_DRIVE_BATCH, ("GET", "/drive/v3/files/a%7eb%2dc%2Ed%5fe%41%7a%30?alt=media", None, None), 302, 404, "download/drive/v3/files/a~b-c.d_eAz0?alt=media&quotaUser=7"),
    (_DRIVE_BATCH, ("GET", "/drive/v3/files/a%E2%82%ACb?alt=media", None, None), 302, 404, "download/drive/v3/files/a%E2%82%ACb?alt=media&quotaUser=7"),
    (_DRIVE_BATCH, ("GET", "/drive/v3/files/a%c3%a9b%2a?alt=media", None, None), 302, 404, "download/drive/v3/files/a%C3%A9b%2A?alt=media&quotaUser=7"),
    (_DRIVE_BATCH, ("GET", "/drive/v3/files/a%23b/export?mimeType=text/plain", None, None), 302, 404, "download/drive/v3/files/a%23b/export?mimeType=text/plain&quotaUser=7"),
    (_DRIVE_BATCH, ("GET", "/drive/v3/files/a%b%4?alt=media", None, None), 302, None, "download/drive/v3/files/a%b%4?alt=media&quotaUser=7"),
    # its place among `$.xgafv`, the credential and `callback`, as `_drive_batch_redirect` records
    (_DRIVE_BATCH, ("GET", "/drive/v3/files/{doc}/export?mimeType=text/plain", None, _BAD), 401, 401, _CHALLENGED),
    (_DRIVE_BATCH, ("GET", "/drive/v3/files/{pdf}?alt=media", None, "OAuth nope"), 401, 401, _CHALLENGED),
    (_DRIVE_BATCH, ("GET", "/drive/v3/files/{pdf}?alt=media", None, "Bearer nope x"), 401, 401, _CHALLENGED),
    (_DRIVE_BATCH, ("GET", "/drive/v3/files/{pdf}?alt=media", None, "Bearernope"), 401, 401, _CHALLENGE),
    (_DRIVE_BATCH, ("GET", "/drive/v3/files/{pdf}?alt=media", None, "Basic YWJjOmRlZg=="), 302, 401, "download/drive/v3/files/{pdf}?alt=media&quotaUser=7"),
    (_DRIVE_BATCH, ("GET", "/drive/v3/files/{pdf}?alt=media", None, "bearer nope"), 302, 401, "download/drive/v3/files/{pdf}?alt=media&quotaUser=7"),
    (_DRIVE_BATCH, ("GET", "/drive/v3/files/{pdf}?alt=media", None, "Bearer"), 302, 401, "download/drive/v3/files/{pdf}?alt=media&quotaUser=7"),
    (_DRIVE_BATCH, ("GET", "/drive/v3/files/{pdf}?alt=media", None, "nope"), 302, 401, "download/drive/v3/files/{pdf}?alt=media&quotaUser=7"),
    # a token in the part's query or the batch's (`_drive_batch_redirect`, `_query_credential`)
    (_DRIVE_BATCH, ("GET", "/drive/v3/files/{pdf}?alt=media&access_token=nope", None, _ANON), 400, None, gerr.QUERY_TOKEN_MESSAGE),
    (_DRIVE_BATCH, ("GET", "/drive/v3/files/{pdf}?alt=media&access_token=", None, _ANON), 400, 400, gerr.QUERY_TOKEN_MESSAGE),
    (_DRIVE_BATCH, ("GET", "/drive/v3/files/{doc}/export?mimeType=text/plain&access_token=nope", None, _ANON), 400, 400, gerr.QUERY_TOKEN_MESSAGE),
    (_DRIVE_BATCH, ("GET", "/drive/v3/files/{pdf}?alt=media&access_token=ya29.x", None, _ANON), 401, 401, _CHALLENGED),
    (f"{_DRIVE_BATCH}&access_token=nope", ("GET", "/drive/v3/files/{pdf}?alt=media", None, _ANON), 400, None, gerr.QUERY_TOKEN_MESSAGE),
    (f"{_DRIVE_BATCH}&ACCESS_TOKEN=x", ("GET", "/drive/v3/files/{doc}/export?mimeType=text/plain", None, _ANON), 400, None, gerr.QUERY_TOKEN_MESSAGE),
    (f"{_DRIVE_BATCH}&bearer_token=x", ("GET", "/drive/v3/files/{doc}/export?mimeType=text/plain", None, _ANON), 400, None, gerr.QUERY_TOKEN_MESSAGE),
    (f"{_DRIVE_BATCH}&access_token=ya29.x", ("GET", "/drive/v3/files/{pdf}?alt=media", None, _ANON), 401, None, _CHALLENGED),
    (f"{_DRIVE_BATCH}&access_token=ya29.x", ("GET", "/drive/v3/files", None, _ANON), 401, None, _CHALLENGED),
    (f"{_DRIVE_BATCH}&access_token=nope", ("GET", "/drive/v3/files", None, _ANON), 403, None, None),
    (f"{_DRIVE_BATCH}&access_token=ya29.x", ("GET", "/drive/v3/files?access_token=nope", None, _ANON), 403, None, None),
    (f"{_DRIVE_BATCH}&access_token=ya29.x", ("GET", "/drive/v3/files", None, None), 200, None, None),
    (f"{_DRIVE_BATCH}&access_token=nope", ("GET", "/drive/v3/files", None, "Bearernope"), 401, None, _CHALLENGED),
    # a name the part carries is read from the part, any other from the batch
    (_DRIVE_BATCH + "&access_token=nope", ("GET", "/drive/v3/files?bearer_token=ya29.x&oauth_token={t}", None, _ANON), 403, None, None),
    (_DRIVE_BATCH + "&oauth_token={t}", ("GET", "/drive/v3/files?access_token=ya29.x", None, _ANON), 200, None, None),
    (_DRIVE_BATCH + "&access_token=ya29.x", ("GET", "/drive/v3/files?oauth_token=nope", None, _ANON), 401, None, _CHALLENGED),
    (_DRIVE_BATCH + "&access_token=ya29.x&oauth_token={t}", ("GET", "/drive/v3/files?oauth_token=nope", None, _ANON), 401, None, _CHALLENGED),
    (_DRIVE_BATCH + "&access_token=ya29.x", ("GET", "/drive/v3/files?access_token=ya29.y", None, _ANON), 401, None, _CHALLENGED),
    (_DRIVE_BATCH + "&access_token=ya29.x", ("GET", "/drive/v3/files/{doc}/export?mimeType=text/plain&oauth_token=nope", None, _ANON), 401, None, _CHALLENGED),
    (_DRIVE_BATCH, ("GET", "/drive/v3/files/{doc}/export?mimeType=text/plain", None, _ANON), 302, 403, "download/drive/v3/files/{doc}/export?mimeType=text/plain&quotaUser=7"),
    (_DRIVE_BATCH, ("GET", "/drive/v3/files/{pdf}?alt=media", None, _ANON), 302, 403, "download/drive/v3/files/{pdf}?alt=media&quotaUser=7"),
    (_DRIVE_BATCH, ("GET", "/drive/v3/files/{doc}", None, _ANON), 403, 403, None),
    (_DRIVE_BATCH, ("GET", "/drive/v3/files/{doc}/export?mimeType=text/plain&callback=cb", None, None), 302, 200, "download/drive/v3/files/{doc}/export?mimeType=text/plain&callback=cb&quotaUser=7"),
    (_DRIVE_BATCH, ("GET", "/drive/v3/files/{doc}/export?mimeType=text/plain&callback=a%20b", None, None), 302, None, "download/drive/v3/files/{doc}/export?mimeType=text/plain&callback=a%20b&quotaUser=7"),
    (_DRIVE_BATCH, ("GET", "/drive/v3/files/{pdf}?alt=media&callback=a%20b", None, None), 302, None, "download/drive/v3/files/{pdf}?alt=media&callback=a%20b&quotaUser=7"),
    (_DRIVE_BATCH, ("GET", "/drive/v3/files/{doc}/export?mimeType=text/plain&callback=a%20b&$.xgafv=9", None, None), 400, None, None),
    # what is not a download is answered as it is on its own, the one part of its batch here (a part
    # beside others is in `_BATCH_ACK_ROWS`)
    (_DRIVE_BATCH, ("GET", "/drive/v3/files/{pdf}?alt=json&alt=media", None, None), 200, 200, None),
    (_DRIVE_BATCH, ("GET", "/drive/v3/files/{doc}?acknowledgeAbuse=TRUE", None, None), 403, 403, None),
    (_DRIVE_BATCH, ("GET", "/drive/v3/files/{nope}?acknowledgeAbuse=true", None, None), 403, 403, None),
    (_DRIVE_BATCH, ("GET", "/drive/v3/files?pageSize=1&includeItemsFromAllDrives=true", None, None), 403, 403, None),
    (_DRIVE_BATCH, ("GET", "/drive/v3/files/{doc}/permissions?useDomainAdminAccess=true", None, None), 404, 404, None),
    (_DRIVE_BATCH, ("GET", "/drive/v3/drives?useDomainAdminAccess=true", None, None), 403, 403, None),
    # a Sheets read is not implemented, at the point `_workbook` records
    (_SHEETS_BATCH, ("GET", "/sheets/v4/spreadsheets/{sheet}/values/A1", None, None), 501, 200, _UNIMPLEMENTED),
    (_SHEETS_BATCH, ("GET", "/sheets/v4/spreadsheets/{sheet}", None, None), 501, 200, _UNIMPLEMENTED),
    (_SHEETS_BATCH, ("GET", "/sheets/v4/spreadsheets/{sheet}/values:batchGet?ranges=A1", None, None), 501, 200, _UNIMPLEMENTED),
    (_SHEETS_BATCH, ("POST", "/sheets/v4/spreadsheets/{sheet}:getByDataFilter", "{}", None), 501, 200, _UNIMPLEMENTED),
    (_SHEETS_BATCH, ("POST", "/sheets/v4/spreadsheets/{sheet}/values:batchGetByDataFilter", _A1_FILTER, None), 501, 200, _UNIMPLEMENTED),
    (_SHEETS_BATCH, ("GET", "/sheets/v4/spreadsheets/{nope}/values/A1", None, None), 501, 404, _UNIMPLEMENTED),
    (_SHEETS_BATCH, ("GET", "/sheets/v4/spreadsheets/{sheet}/values/A1", None, _MIA), 501, 403, _UNIMPLEMENTED),
    (_SHEETS_BATCH, ("GET", "/sheets/v4/spreadsheets/{sheet}/values/NoSuchSheet!A1", None, None), 501, 400, _UNIMPLEMENTED),
    (_SHEETS_BATCH, ("GET", "/sheets/v4/spreadsheets/{sheet}/values/A1?alt=media", None, None), 501, 400, _UNIMPLEMENTED),
    (_SHEETS_BATCH, ("GET", "/sheets/v4/spreadsheets/{sheet}/values/A1?$.xgafv=1", None, None), 501, 200, _UNIMPLEMENTED_AT_XGAFV_1),
    (_SHEETS_BATCH, ("GET", "/sheets/v4/spreadsheets/{sheet}/values/A1?majorDimension=NOPE", None, None), 400, 400, None),
    (_SHEETS_BATCH, ("GET", "/sheets/v4/spreadsheets/{sheet}/values/A1?fields=bogus", None, None), 400, 400, None),
    (_SHEETS_BATCH, ("GET", "/sheets/v4/spreadsheets/{nope}/values/A1?fields=bogus", None, None), 400, 400, None),
    (_SHEETS_BATCH, ("POST", "/sheets/v4/spreadsheets/{nope}:getByDataFilter?fields=bogus", "{}", None), 400, 400, None),
    (_SHEETS_BATCH, ("POST", "/sheets/v4/spreadsheets/{sheet}:getByDataFilter", _BAD_FILTER, None), 400, 400, None),
    (_SHEETS_BATCH, ("POST", "/sheets/v4/spreadsheets/{sheet}/values:batchGetByDataFilter", _BAD_FILTER, None), 400, 400, None),
    (_SHEETS_BATCH, ("GET", "/sheets/v4/spreadsheets/{sheet}/values/A1", None, _BAD), 401, 401, _CHALLENGED),
    (_SHEETS_BATCH, ("POST", "/sheets/v4/spreadsheets/{sheet}:getByDataFilter", "{}", _BAD), 401, 401, _CHALLENGED),
    (_SHEETS_BATCH, ("POST", "/sheets/v4/spreadsheets/{sheet}:getByDataFilter", _BAD_FILTER, _BAD), 401, 401, _CHALLENGED),
]
# fmt: on


def _batch_answer(client, headers, uri, method, target, body, auth):
    """The one part of a one-part batch, as ``(status, header lines, body)``."""
    head = f"{method} {target} HTTP/1.1\r\n"
    if auth:
        head += f"Authorization: {auth}\r\n"
    if body is not None:
        head += f"Content-Type: application/json\r\n\r\n{body}"
    payload = (
        f"--b\r\nContent-Type: application/http\r\nContent-ID: <p0>\r\n\r\n{head}\r\n--b--\r\n"
    )
    r = client.post(
        uri, headers={**headers, "Content-Type": "multipart/mixed; boundary=b"}, content=payload
    )
    assert r.status_code == 200, r.text
    part = r.text.split("\r\n\r\n", 1)[1].rsplit("\r\n--", 1)[0]
    sub_head, _, sub_body = part.partition("\r\n\r\n")
    status_line, *lines = sub_head.split("\r\n")
    return int(status_line.split(" ")[1]), lines, sub_body


@pytest.mark.parametrize("uri, part, status, alone, detail", _BATCH_ROWS)
def test_google_batch_redirects_a_drive_download_and_refuses_a_sheets_read(
    client, admin_h, tokens, tokens_yaml, uri, part, status, alone, detail
):
    """The rows `_BATCH_ROWS` records, each beside the same request sent on its own where the row
    gives that request's status. A part the redirect answers carries real's three headers in real's
    order, and a 401 its challenge ahead of its `Content-Type`. Neither the redirect nor the 501
    looks the file up, so a spreadsheet the scoped token cannot see is answered as one that does not
    exist is."""
    ids = {
        "doc": _drive_find(client, admin_h, "Brand")["id"],
        "pdf": _drive_find(client, admin_h, "Whitepaper")["id"],
        "sheet": _drive_find(client, admin_h, "Q1 Revenue Model")["id"],
        "nope": "nosuchfile000",
        "t": tokens_yaml["admin_token"],
    }
    method, target, body, auth = part
    target = target.format(**ids)
    uri = uri.format(**ids)
    outer = {} if auth == _ANON else admin_h
    auth = None if auth == _ANON else auth and auth.format(mia=f"Bearer {tokens['mia@acme.com']}")
    got, lines, sub_body = _batch_answer(client, outer, uri, method, target, body, auth)
    assert got == status, sub_body
    if status == 302:
        location = f"Location: http://testserver/{detail.format(**ids)}"
        assert lines == [
            "Content-Length: 0",
            "Content-Type: application/json; charset=UTF-8",
            location,
        ]
        assert json.loads(sub_body) == _REDIRECTED
    elif status == 401:
        assert lines[:2] == [
            f"WWW-Authenticate: {detail}",
            "Content-Type: application/json; charset=UTF-8",
        ]
    elif status == 400 and detail is not None:
        assert json.loads(sub_body)["error"]["message"] == detail
    elif status == 501:
        assert json.loads(sub_body) == detail
    if alone is not None:
        headers = {"Authorization": auth} if auth else outer
        sent = client.request(method, target, headers=headers, content=body)
        assert sent.status_code == alone, sent.text


def test_google_batch_passes_on_no_location_on_the_host_it_sends_parts_to(client, admin_h):
    """`batch` sends its parts to a host nothing answers, so a redirect a route builds from it is
    passed on without its `Location`. A function of its own: a GitHub part is no answer real's
    Drive batch gives, so it is no row of `_BATCH_ROWS`."""
    target = "/github/repos/acme/gateway/contents/src/"
    alone = client.get(target, headers=admin_h, follow_redirects=False)
    assert (alone.status_code, "location" in alone.headers) == (302, True)
    status, lines, _ = _batch_answer(client, admin_h, _DRIVE_BATCH, "GET", target, None, None)
    assert (status, lines) == (302, ["Content-Type: text/html;charset=utf-8"])


# A Drive batch part whose path carries a `;`, as real's Drive batch answered each one. A row is
# the part's target, the status in the batch and what the answer carries: a 302's `Location`
# below the server's base URL, a 404's message, or the top-level key a 200 carries. The last row
# is the escaped spelling, which stays in the path. `{doc}` is a Doc's id.
_BATCH_CUT_ROWS = [
    ("/drive/v3/files/{doc};x?fields=id,name", 200, "id"),
    ("/drive/v3/files/{doc};a=b;c?fields=id,name", 200, "id"),
    ("/drive/v3/files/{doc};x/export?mimeType=text/plain", 200, "id"),
    (
        "/drive/v3/files/{doc}/export;x?mimeType=text/plain",
        302,
        "download/drive/v3/files/{doc}/export?mimeType=text/plain&quotaUser=7",
    ),
    ("/drive/v3/files/{doc}/permissions;x?fields=permissions(role)", 200, "permissions"),
    ("/drive/v3/files;x?pageSize=1&fields=files(id)", 200, "files"),
    ("/drive/v3/about;x?fields=user", 200, "user"),
    ("/drive/v3/drives;x", 200, "drives"),
    ("/drive/v3/files/nosuch;x", 404, "File not found: nosuch."),
    ("/drive/v3/files/{doc}%3Bx?fields=id,name", 404, "File not found: {doc};x."),
]


@pytest.mark.parametrize("target, status, detail", _BATCH_CUT_ROWS)
def test_google_drive_batch_cuts_a_part_path_at_a_semicolon(
    client, admin_h, target, status, detail
):
    """The rows `_BATCH_CUT_ROWS` records, each as the one part of its batch
    (`_drive_batch_cut_path`), and the same request on its own, which no batch cuts."""
    doc = _drive_find(client, admin_h, "Brand")["id"]
    ids = {"doc": doc}
    got, lines, sub_body = _batch_answer(
        client, admin_h, _DRIVE_BATCH, "GET", target.format(**ids), None, None
    )
    assert got == status, sub_body
    if status == 302:
        assert lines == [
            "Content-Length: 0",
            "Content-Type: application/json; charset=UTF-8",
            f"Location: http://testserver/{detail.format(**ids)}",
        ]
        assert json.loads(sub_body) == _REDIRECTED
    elif status == 404:
        assert json.loads(sub_body)["error"]["message"] == detail.format(**ids)
    else:
        body = json.loads(sub_body)
        assert detail in body, sub_body
        if detail == "id":
            assert body["id"] == doc and body["name"]
    assert client.get(target.format(**ids), headers=admin_h).status_code == 404


def test_google_drive_batch_cuts_every_part_path_at_a_semicolon(client, admin_h):
    """All the `_BATCH_CUT_ROWS` targets in one batch, as they were measured in one batch, each
    part cut on its own."""
    doc = _drive_find(client, admin_h, "Brand")["id"]
    payload = "".join(
        f"--b\r\nContent-Type: application/http\r\nContent-ID: <p{i}>\r\n\r\n"
        f"GET {target.format(doc=doc)} HTTP/1.1\r\n\r\n"
        for i, (target, _, _) in enumerate(_BATCH_CUT_ROWS)
    )
    r = client.post(
        _DRIVE_BATCH,
        headers={**admin_h, "Content-Type": "multipart/mixed; boundary=b"},
        content=payload + "--b--\r\n",
    )
    assert r.status_code == 200, r.text
    assert [int(code) for code in re.findall(r"^HTTP/1\.1 (\d+)", r.text, re.M)] == [
        status for _, status, _ in _BATCH_CUT_ROWS
    ]


_ACK = "/drive/v3/files/{doc}?acknowledgeAbuse=true&fields=id"
_ACK_NOPE = "/drive/v3/files/{nope}?acknowledgeAbuse=true"
_DOWNLOAD = "/drive/v3/files/{pdf}?alt=media"
_EXPORT = "/drive/v3/files/{doc}/export?mimeType=text/plain"
_ABOUT = "/drive/v3/about?fields=user"
_SHARED = "/drive/v3/files?pageSize=1&includeItemsFromAllDrives=true"

# Batches of several parts, one or two of them asking `acknowledgeAbuse` on a read that downloads
# nothing, with the status of each part as real answered it. The flag is checked where its part is
# the one part of the batch that is not a download (`drive_files_get`).
_BATCH_ACK_ROWS = [
    ([_ACK, _ABOUT], [200, 200]),
    ([_ABOUT, _ACK], [200, 200]),
    ([_ACK, _ACK], [200, 200]),
    ([_ACK_NOPE, _ABOUT], [404, 200]),
    ([_SHARED, _ACK], [403, 200]),
    ([_DOWNLOAD, _ACK, _ACK], [302, 200, 200]),
    ([_ABOUT, _DOWNLOAD, _ACK], [200, 302, 200]),
    ([_DOWNLOAD, _ACK], [302, 403]),
    ([_ACK, _DOWNLOAD], [403, 302]),
    ([_EXPORT, _ACK], [302, 403]),
    ([_DOWNLOAD, _ACK_NOPE], [302, 403]),
    ([_DOWNLOAD, _DOWNLOAD, _ACK], [302, 302, 403]),
]


@pytest.mark.parametrize("targets, statuses", _BATCH_ACK_ROWS)
def test_google_batch_checks_acknowledge_abuse_on_its_one_part_that_is_not_a_download(
    client, admin_h, targets, statuses
):
    """The rows `_BATCH_ACK_ROWS` records. A function of its own: a row is a batch of several
    parts, where a `_BATCH_ROWS` row is one part."""
    ids = {
        "doc": _drive_find(client, admin_h, "Brand")["id"],
        "pdf": _drive_find(client, admin_h, "Whitepaper")["id"],
        "nope": "nosuchfile000",
    }
    payload = "".join(
        f"--b\r\nContent-Type: application/http\r\nContent-ID: <p{i}>\r\n\r\n"
        f"GET {target.format(**ids)} HTTP/1.1\r\n\r\n"
        for i, target in enumerate(targets)
    )
    r = client.post(
        _DRIVE_BATCH,
        headers={**admin_h, "Content-Type": "multipart/mixed; boundary=b"},
        content=payload + "--b--\r\n",
    )
    assert r.status_code == 200, r.text
    assert [int(code) for code in re.findall(r"^HTTP/1\.1 (\d+)", r.text, re.M)] == statuses


def test_user_cannot_fetch_others_private_gmail(client, tokens_yaml, admin_h, ro_conn):
    # a private gmail doc owned by user B, fetched with user A's token -> 404
    user_a, user_b = tokens_yaml["users"][0], tokens_yaml["users"][1]
    doc = ro_conn.execute(
        "SELECT id FROM gmail_messages WHERE author_email=? LIMIT 1",
        (user_b["email"],),
    ).fetchone()
    if doc is None:
        pytest.skip("no gmail doc for user B in this subset")

    hexid = doc["id"]  # served ids are hex, not dsids
    ah = {"Authorization": f"Bearer {user_a['token']}"}
    r = client.get(f"/gmail/v1/users/me/messages/{hexid}", headers=ah)
    # A may coincidentally be a recipient; assert admin can always read it
    assert client.get(f"/gmail/v1/users/me/messages/{hexid}", headers=admin_h).status_code == 200
    assert r.status_code in (200, 404)


# --- Google error envelope ------------------------------------------------------------
#
# Every case below was MEASURED against the live APIs with real OAuth credentials. The envelope is
# per-family, not uniform: which optional members each family carries is the table in the
# `backlot.errors.google` docstring. What a request with no credential gets is per operation
# (`errors.google.no_credentials`), and a bad token is 401 UNAUTHENTICATED in every family.


def _gerr(resp):
    """The `error` object, or a clear failure naming what came back instead."""
    body = resp.json()
    assert "error" in body, f"expected a Google error envelope, got {body}"
    return body["error"]


def test_google_errors_use_googles_envelope(client, admin_h):
    """`google-api-python-client` reads `error.message` to build HttpError, so `{"detail": …}` left
    every error unreadable to the one client Backlot exists to serve."""
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
    # a well-formed but unknown id; a non-hex one is 400 "Invalid id value"
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


@pytest.mark.parametrize(
    "name, caller, mime, answer",
    [
        # no `mimeType`: a document, a PDF, a file that does not exist, and a spreadsheet the scoped
        # token cannot see, which with a `mimeType` is its 404
        ("Brand", None, None, "required"),
        ("Whitepaper", None, None, "required"),
        (None, None, None, "required"),
        ("Q1 Revenue Model", "mia@acme.com", None, "required"),
        # a format the spreadsheet does not export to
        ("Q1 Revenue Model", None, "text/plain", "unsupported"),
        ("Q1 Revenue Model", None, "bogus/type", "unsupported"),
        ("Q1 Revenue Model", None, "application/vnd.google-apps.document", "unsupported"),
        ("Q1 Revenue Model", None, "", "unsupported"),
        ("Q1 Revenue Model", None, "text/csv ", "unsupported"),
        ("Q1 Revenue Model", None, "text/csv;charset=utf-8", "unsupported"),
        # a format its type exports to, in any case, and the one it answers as
        ("Q1 Revenue Model", None, "text/csv", "text/csv"),
        ("Q1 Revenue Model", None, "TEXT/CSV", "text/csv"),
        ("Q1 Revenue Model", None, "Text/Csv", "text/csv"),
        ("Brand", None, "text/markdown", "text/markdown"),
        ("Brand", None, "TEXT/MARKDOWN", "text/markdown"),
    ],
)
def test_drive_export_answers_a_mime_type_the_way_real_does(
    client, admin_h, tokens, name, caller, mime, answer
):
    """The order and the matching `drive_files_export`'s docstring records. `required` is the
    absent-`mimeType` refusal, `unsupported` the one `gerr.unsupported_conversion` records, and a
    format the export that format answers, served under the `mimeType` exactly as sent. With a
    `mimeType`, a file that does not exist is its 404. The TSV spelling is
    `test_tsv_export_reserialises_the_grid_rather_than_serving_content`'s, over a grid."""
    fid = _drive_find(client, admin_h, name)["id"] if name else "nosuchfileid123"
    headers = {"Authorization": f"Bearer {tokens[caller]}"} if caller else admin_h
    url = f"/drive/v3/files/{fid}/export"
    r = client.get(url, headers=headers, params={} if mime is None else {"mimeType": mime})
    if answer == "required":
        e = _gerr(r)
        assert e["code"] == 400 and e["message"] == "Required parameter: mimeType"
        assert e["errors"][0] == {
            "message": "Required parameter: mimeType",
            "domain": "global",
            "reason": "required",
            "location": "mimeType",
            "locationType": "parameter",
        }
        if caller:
            ok = client.get(url, headers=headers, params={"mimeType": "text/csv"})
            assert ok.status_code == 404
        return
    if answer == "unsupported":
        e = _gerr(r)
        assert e["code"] == 400
        assert e["errors"] == [
            {
                "message": "The requested conversion is not supported.",
                "domain": "global",
                "reason": "badRequest",
                "location": "convertTo",
                "locationType": "parameter",
            }
        ]
    else:
        assert r.status_code == 200, r.text
        assert r.headers["content-type"] == mime
        assert r.text == client.get(url, headers=admin_h, params={"mimeType": answer}).text
    missing = client.get(
        "/drive/v3/files/nosuchfileid123/export", headers=admin_h, params={"mimeType": mime}
    )
    assert missing.status_code == 404


@pytest.mark.parametrize(
    "path, top",
    [
        ("/drive/v3/files", 1000),
        ("/drive/v3/files/{doc}/permissions", 100),
        ("/drive/v3/drives", 100),
    ],
)
@pytest.mark.parametrize(
    "values, kind, named",
    [
        (["0"], "range", "0"),
        (["-1"], "range", "-1"),
        (["-0"], "range", "0"),
        (["{above}"], "range", "{above}"),
        (["2147483647"], "range", "2147483647"),
        (["2147483648"], "int32", "2147483648"),
        ([" 2"], "int32", " 2"),
        (["2 "], "int32", "2 "),
        (["1_0"], "int32", "1_0"),
        (["2.0"], "int32", "2.0"),
        (["0x10"], "int32", "0x10"),
        (["1e2"], "int32", "1e2"),
        ([""], "int32", ""),
        (["NOPE"], "int32", "NOPE"),
        (["2", "NOPE"], "int32", "NOPE"),
        (["NOPE", "2"], "int32", "NOPE"),
        (["+2"], "size", "2"),
        (["02"], "size", "2"),
        (["1"], "size", "1"),
        (["{top}"], "size", "{top}"),
    ],
)
def test_drive_a_listing_takes_an_int32_page_size_from_1_to_its_top(
    client, admin_h, path, top, values, kind, named
):
    """The rules `_INT32` and `_drive_page_size_in_range` record, on each route, each value alone
    unless the row lists two. `range` is the range refusal naming the value as an int, `int32` the
    proto layer's `TYPE_INT32` one quoting it, and `size` a 200, which lists that many files on
    `files.list` and that many permissions on `permissions.list`, or all there are, and none on
    `drives.list`."""
    fill = {"top": top, "above": top + 1}
    named = named.format(**fill)
    url = path.format(doc=_drive_find(client, admin_h, "Brand")["id"])
    r = client.get(url, headers=admin_h, params=[("pageSize", v.format(**fill)) for v in values])
    if kind == "size":
        assert r.status_code == 200, r.text
        listed = {"/drive/v3/files": "files", _PERMS: "permissions"}.get(path)
        if listed:
            total = len(client.get(url, headers=admin_h, params={"pageSize": top}).json()[listed])
            assert len(r.json()[listed]) == min(int(named), total)
        return
    e = _gerr(r)
    assert e["code"] == 400
    if kind == "range":
        assert e["errors"] == [
            {
                "message": (
                    f"Invalid value '{named}'. Values must be within the range: [value: 1\n, "
                    f"value: {top}\n]"
                ),
                "domain": "global",
                "reason": "invalidParameter",
                "location": "page_size",
                "locationType": "parameter",
            }
        ]
        assert "status" not in e and "details" not in e
    else:
        message = f"Invalid value at 'page_size' (TYPE_INT32), \"{named}\""
        assert e["errors"] == [{"message": message, "reason": "invalid"}]
        assert e["status"] == "INVALID_ARGUMENT"
        assert e["details"][0]["fieldViolations"] == [
            {"field": "page_size", "description": message}
        ]


@pytest.mark.parametrize(
    "query, size",
    [
        ("pageSize=0&pageSize=2", 500),
        ("pageSize=-1&pageSize=2", 500),
        ("pageSize=0&pageSize=0", 500),
        ("pageSize=0&pageSize=1001", 500),
        ("pageSize=1001&pageSize=2", 1000),
        ("pageSize=1001&pageSize=1001", 1000),
        ("pageSize=2&pageSize=1001", 2),
        ("pageSize=3&pageSize=0", 3),
        ("pageSize=2&pageSize=0", 2),
        ("pageSize=7", 7),
        ("", 100),
    ],
)
@pytest.mark.parametrize("max_page_size", [1000, 2000, 5])
def test_a_repeated_page_size_is_read_first_and_not_range_checked(
    monkeypatch, query, size, max_page_size
):
    """Pins `_drive_page_size`'s measurement, read off the reader because the bundled corpus holds
    fewer than 500 files and a listing could not tell the sizes apart. A deployment's
    `max_page_size` caps the result on top of real's own rule; an absent `pageSize` is the default
    whatever the cap."""
    from types import SimpleNamespace

    from starlette.requests import Request

    from backlot.routers import google

    monkeypatch.setattr(
        google,
        "get_settings",
        lambda: SimpleNamespace(default_page_size=100, max_page_size=max_page_size),
    )
    request = Request({"type": "http", "query_string": query.encode(), "headers": []})
    sizes = google._drive_typed(request, page_size=True)["pageSize"]
    assert google._drive_page_size(sizes) == (min(size, max_page_size) if query else size)


def test_drive_a_page_token_it_did_not_issue_is_refused(client, admin_h):
    """The `pageToken` rule `drive_files_list`'s comment records, on `BOGUS`, beside an empty token
    and the one a listing issued, which is the next page."""
    files = "/drive/v3/files"
    e = _gerr(client.get(files, headers=admin_h, params={"pageToken": "BOGUS"}))
    assert e["code"] == 400
    assert e["errors"] == [
        {
            "message": "Invalid Value",
            "domain": "global",
            "reason": "invalid",
            "location": "pageToken",
            "locationType": "parameter",
        }
    ]
    assert "status" not in e
    first = client.get(files, headers=admin_h, params={"pageSize": 1}).json()
    empty = client.get(files, headers=admin_h, params={"pageSize": 1, "pageToken": ""}).json()
    assert empty == first
    token = client.get(
        files, headers=admin_h, params={"pageSize": 1, "pageToken": first["nextPageToken"]}
    )
    assert token.status_code == 200 and token.json()["files"] != first["files"]


_PERMS = "/drive/v3/files/{doc}/permissions"
_TOKEN_INVALID = {
    "code": 400,
    "message": "Invalid Value",
    "errors": [
        {
            "message": "Invalid Value",
            "domain": "global",
            "reason": "invalid",
            "location": "pageToken",
            "locationType": "parameter",
        }
    ],
}
_TOKEN_EXPIRED = {
    "code": 403,
    "message": "The specified page token has expired, and can no longer be used.",
    "errors": [
        {
            "message": "The specified page token has expired, and can no longer be used.",
            "domain": "global",
            "reason": "pageTokenExpired",
        }
    ],
}
_UNKNOWN_ERROR = {"code": 500, "message": "Unknown Error.", "errors": [{}], "status": "UNKNOWN"}


@pytest.mark.parametrize(
    "path, query, error",
    [
        *[
            (path, query, error)
            for path in (_PERMS, "/drive/v3/drives")
            for query, error in [
                ([("pageToken", "bad")], _TOKEN_INVALID),
                ([("pageToken", "bzow")], _TOKEN_INVALID),
                ([("pageToken", "{token}")], _TOKEN_INVALID),
                ([("useDomainAdminAccess", "true"), ("pageToken", "bad")], _TOKEN_INVALID),
                ([("pageToken", "bad"), ("useDomainAdminAccess", "true")], _TOKEN_INVALID),
                ([("pageToken", "bad"), ("pageSize", "0")], None),
                ([("pageToken", "bad"), ("useDomainAdminAccess", "NOPE")], None),
            ]
        ],
        (_PERMS, [("pageToken", "")], _TOKEN_EXPIRED),
        (_PERMS, [("useDomainAdminAccess", "true"), ("pageToken", "")], _TOKEN_EXPIRED),
        ("/drive/v3/files/nosuchfileid000000/permissions", [("pageToken", "")], _TOKEN_EXPIRED),
        ("/drive/v3/files/nosuchfileid000000/permissions", [("pageToken", "bzow")], _TOKEN_INVALID),
        (_PERMS, [("pageSize", "1"), ("pageToken", "{issued}.")], _TOKEN_INVALID),
        (_PERMS, [("pageSize", "1"), ("pageToken", "{at_zero}")], _TOKEN_INVALID),
        ("/drive/v3/drives", [("pageToken", "")], None),
        ("/drive/v3/drives", [("useDomainAdminAccess", "true"), ("pageToken", "")], None),
    ],
)
def test_drive_permissions_and_drives_refuse_a_page_token_they_did_not_issue(
    client, admin_h, path, query, error
):
    """The rules `_drive_permissions_page_token` and `_drive_listing_page_token` record, one request
    per row. `None` is a token that changes nothing: the answer is the one the request gets without
    it, a page or the refusal of the value beside it. `{token}` is filled from the first page of
    `files.list`, `{issued}` from the first page of this file's `permissions.list` at `pageSize=1`,
    and `{at_zero}` is `_drive_permissions_token`'s spelling for an offset of 0, which it never
    issues."""
    from backlot.routers import google

    doc = _drive_find(client, admin_h, "Brand")["id"]
    url = path.format(doc=doc)
    files = client.get("/drive/v3/files", headers=admin_h, params={"pageSize": 1}).json()
    issued = client.get(_PERMS.format(doc=doc), headers=admin_h, params={"pageSize": 1}).json()
    fill = {
        "token": files["nextPageToken"],
        "issued": issued["nextPageToken"],
        "at_zero": google._drive_permissions_token(doc, 0),
    }
    query = [(k, v.format(**fill)) for k, v in query]
    r = client.get(url, headers=admin_h, params=query)
    if error is None:
        without = [(k, v) for k, v in query if k != "pageToken"]
        assert r.content == client.get(url, headers=admin_h, params=without).content
    else:
        assert (r.status_code, _gerr(r)) == (error["code"], error)


# fmt: off
_PERMISSION_PAGES = [
    # the first request's query, the query its token is sent back with, and the length of each page
    # until one carries no token, or the error the first request is refused with
    ([], [], [2]),
    ([("pageSize", "1")], [("pageSize", "1")], [1, 1]),
    ([("pageSize", "1")], [("pageSize", "2")], [1, 1]),
    ([("pageSize", "1")], [], [1, 1]),
    ([("pageSize", "2")], [], [2]),
    ([("pageSize", "3")], [], [2]),
    ([("pageSize", "100")], [], [2]),
    # two or more values, read from the first
    ([("pageSize", "1"), ("pageSize", "0")], [], [1, 1]),
    ([("pageSize", "+1"), ("pageSize", "0")], [], [1, 1]),
    ([("pageSize", "1"), ("pageSize", "2")], [], [1, 1]),
    ([("pageSize", "1"), ("pageSize", "2"), ("pageSize", "3")], [], [1, 1]),
    ([("pageSize", "100"), ("pageSize", "2")], [], [2]),
    ([("pageSize", "2"), ("pageSize", "0"), ("pageSize", "0")], [], [2]),
    # and the 500 when the first is outside 1-100, after the `pageToken` refusals
    ([("pageSize", "0"), ("pageSize", "2")], [], _UNKNOWN_ERROR),
    ([("pageSize", "-0"), ("pageSize", "2")], [], _UNKNOWN_ERROR),
    ([("pageSize", "-1"), ("pageSize", "2")], [], _UNKNOWN_ERROR),
    ([("pageSize", "0"), ("pageSize", "0")], [], _UNKNOWN_ERROR),
    ([("pageSize", "0"), ("pageSize", "101")], [], _UNKNOWN_ERROR),
    ([("pageSize", "101"), ("pageSize", "2")], [], _UNKNOWN_ERROR),
    ([("pageSize", "101"), ("pageSize", "101")], [], _UNKNOWN_ERROR),
    ([("pageSize", "2147483647"), ("pageSize", "2")], [], _UNKNOWN_ERROR),
    ([("pageSize", "-2147483648"), ("pageSize", "2")], [], _UNKNOWN_ERROR),
    ([("pageSize", "0"), ("pageSize", "2"), ("pageSize", "2")], [], _UNKNOWN_ERROR),
    ([("pageSize", "0"), ("pageSize", "2"), ("pageToken", "bad")], [], _TOKEN_INVALID),
    ([("pageToken", "bad"), ("pageSize", "0"), ("pageSize", "2")], [], _TOKEN_INVALID),
    ([("pageSize", "0"), ("pageSize", "2"), ("pageToken", "")], [], _TOKEN_EXPIRED),
]
# fmt: on


@pytest.mark.parametrize("first, follow, expected", _PERMISSION_PAGES)
def test_drive_permissions_list_pages_in_the_unpaged_order(
    client, admin_h, first, follow, expected
):
    """The rules `_drive_permission_page` and `_drive_permissions_page_size` record, on a file with
    two permissions. A row of page lengths also checks that the pages, joined, are the unpaged
    list."""
    url = _PERMS.format(doc=_drive_find(client, admin_h, "Brand")["id"])
    whole = client.get(url, headers=admin_h).json()["permissions"]
    assert len(whole) == 2
    r = client.get(url, headers=admin_h, params=first)
    if isinstance(expected, dict):
        assert (r.status_code, _gerr(r)) == (expected["code"], expected)
        return
    pages = [r]
    while "nextPageToken" in pages[-1].json() and len(pages) < len(expected):
        token = pages[-1].json()["nextPageToken"]
        pages.append(client.get(url, headers=admin_h, params=[*follow, ("pageToken", token)]))
    assert [p.status_code for p in pages] == [200] * len(pages)
    assert [len(p.json()["permissions"]) for p in pages] == expected
    assert "nextPageToken" not in pages[-1].json()
    assert [x for p in pages for x in p.json()["permissions"]] == whole


@pytest.mark.parametrize("size, lengths", [(1, [1, 1, 1]), (2, [2, 1])])
def test_drive_permissions_list_pages_a_file_with_three_permissions(tmp_path, size, lengths):
    """`_drive_permission_page` past the second page: at `pageSize=1` the page a token serves
    carries the token for the third, at `pageSize=2` the second page starts after the two the first
    served, and the pages joined are the unpaged list. The files measured had two permissions
    each, so neither was measured."""
    from tests._helpers import corpus_client

    record = {
        "source_type": "google_drive",
        "doc_id": "three",
        "folder": "mk",
        "title": "Three readers",
        "content": "x",
        "author_email": "a@x.com",
        "readers": ["b@x.com", "c@x.com"],
    }
    with corpus_client(tmp_path, [record]) as (client, settings):
        h = {"Authorization": f"Bearer {settings.admin_token}"}
        q = {"q": "name = 'Three readers'"}
        (doc,) = client.get("/drive/v3/files", headers=h, params=q).json()["files"]
        url = f"/drive/v3/files/{doc['id']}/permissions"
        whole = client.get(url, headers=h).json()["permissions"]
        assert len(whole) == 3
        pages = [client.get(url, headers=h, params={"pageSize": size}).json()]
        while "nextPageToken" in pages[-1] and len(pages) < len(lengths):
            token = pages[-1]["nextPageToken"]
            pages.append(
                client.get(url, headers=h, params={"pageSize": size, "pageToken": token}).json()
            )
    assert [len(p["permissions"]) for p in pages] == lengths
    assert "nextPageToken" not in pages[-1]
    assert [x for p in pages for x in p["permissions"]] == whole


@pytest.mark.parametrize(
    "route, names",
    [
        ("/drive/v3/files/{file_id}/permissions", {"pageSize", "pageToken", "fields"}),
        ("/drive/v3/drives", {"q", "fields"}),
    ],
)
def test_drive_listing_declares_the_parameters_it_reads(client, route, names):
    """A parameter that shapes the answer is declared, so a client reading the OpenAPI document can
    find it, and `backlot diff --source google_drive` reports none of them as missing."""
    op = client.app.openapi()["paths"][route]["get"]
    assert names <= {p["name"] for p in op["parameters"]}


@pytest.mark.parametrize(
    "query, code, location",
    [
        ([("pageToken", "BOGUS"), ("pageSize", "NOPE")], 400, None),
        ([("fields", "bogus"), ("pageSize", "0")], 400, "page_size"),
        ([("fields", "bogus"), ("pageToken", "BOGUS")], 400, "pageToken"),
        ([("pageToken", "BOGUS"), ("fields", "bogus")], 400, "pageToken"),
        ([("pageToken", "BOGUS"), ("q", "nosuchfield = 1")], 400, "q"),
        ([("fields", "bogus"), ("q", "nosuchfield = 1")], 400, "q"),
        ([("q", "nosuchfield = 1"), ("orderBy", "bogus")], 400, "orderBy"),
        ([("orderBy", "bogus"), ("q", "nosuchfield = 1")], 400, "orderBy"),
        ([("fields", "bogus"), ("orderBy", "bogus")], 400, "orderBy"),
        ([("orderBy", "name,name"), ("pageSize", "0")], 400, "page_size"),
        ([("orderBy", "name,name"), ("pageSize", "NOPE")], 400, None),
        ([("q", "nosuchfield = 1"), ("orderBy", "name,name")], 403, "orderBy"),
        ([("orderBy", "name,name"), ("q", "nosuchfield = 1")], 403, "orderBy"),
        ([("pageToken", "BOGUS"), ("orderBy", "name,name")], 403, "orderBy"),
        ([("fields", "bogus"), ("orderBy", "name,name")], 403, "orderBy"),
        ([("orderBy", "name,starred"), ("pageSize", "0")], 400, "page_size"),
        ([("orderBy", "name,starred"), ("q", "nosuchfield = 1")], 400, "q"),
        ([("orderBy", "name,starred"), ("pageToken", "BOGUS")], 400, "pageToken"),
        ([("fields", "bogus"), ("orderBy", "name,starred")], 500, None),
        ([("fields", ""), ("orderBy", "name,starred")], 500, None),
        ([("q", "name = 'no such file'"), ("orderBy", "name,starred")], 500, None),
        (
            [("q", "fullText contains 'the'"), ("orderBy", "name"), ("pageSize", "0")],
            400,
            "page_size",
        ),
        (
            [("q", "fullText contains 'the'"), ("orderBy", "name"), ("pageSize", "NOPE")],
            400,
            None,
        ),
        ([("q", "fullText contains 'the'"), ("orderBy", "bogus")], 400, "orderBy"),
        ([("q", "fullText contains 'the' and nosuchfield = 1"), ("orderBy", "name")], 400, "q"),
        (
            [("q", "fullText contains 'the' and nosuchfield = 1"), ("orderBy", "viewedByMeTime")],
            400,
            "q",
        ),
        (
            [("q", "fullText contains 'the'"), ("orderBy", "name"), ("pageToken", "BOGUS")],
            403,
            "orderBy",
        ),
        (
            [("pageToken", "BOGUS"), ("q", "fullText contains 'the'"), ("orderBy", "name")],
            403,
            "orderBy",
        ),
        (
            [("q", "fullText contains 'the'"), ("orderBy", "name"), ("fields", "bogus")],
            403,
            "orderBy",
        ),
        (
            [("fields", "bogus"), ("q", "fullText contains 'the'"), ("orderBy", "name")],
            403,
            "orderBy",
        ),
        ([("q", "fullText contains 'the'"), ("orderBy", "name"), ("fields", "")], 403, "orderBy"),
        ([("fields", ""), ("q", "fullText contains 'the'"), ("orderBy", "name")], 403, "orderBy"),
    ],
)
def test_drive_files_list_refuses_in_reals_order(client, admin_h, query, code, location):
    """Two values at once, refused in the order `drive_files_list`'s comment records, where a `q`
    with a `fullText` term and an `orderBy` count as one, the 403. A `pageSize` the proto layer
    cannot read has no `location`."""
    e = _gerr(client.get("/drive/v3/files", headers=admin_h, params=query))
    assert e["code"] == code
    assert e["errors"][0].get("location") == location


_DRIVE_BOOL_ROUTES = [
    ("/drive/v3/files", "supportsAllDrives"),
    ("/drive/v3/files", "supportsTeamDrives"),
    ("/drive/v3/files", "includeItemsFromAllDrives"),
    ("/drive/v3/files", "includeTeamDriveItems"),
    ("/drive/v3/files/{doc}", "acknowledgeAbuse"),
    ("/drive/v3/files/{doc}", "supportsAllDrives"),
    ("/drive/v3/files/{doc}", "supportsTeamDrives"),
    ("/drive/v3/files/{doc}/permissions", "supportsAllDrives"),
    ("/drive/v3/files/{doc}/permissions", "supportsTeamDrives"),
    ("/drive/v3/files/{doc}/permissions", "useDomainAdminAccess"),
    ("/drive/v3/drives", "useDomainAdminAccess"),
]

_DRIVE_BOOL_ROWS = (
    [
        (path, param, value, accepted)
        for path, param in _DRIVE_BOOL_ROUTES
        for value, accepted in [
            ("1", True),
            ("y", True),
            ("No", True),
            ("on", False),
            ("", False),
            ("NOPE", False),
        ]
    ]
    + [
        # the rest of the sweep the docstring names
        ("/drive/v3/files", "supportsAllDrives", value, accepted)
        for value, accepted in [
            (v, True) for v in ("true", "FALSE", "tRuE", "0", "t", "F", "Y", "no", "YES")
        ]
        + [(v, False) for v in ("off", "2", "01", "00", "1.0", "-1", "+1", " true", "true ")]
    ]
    + [
        # the letters outside ASCII `_DRIVE_BOOLS` records, which are not case-folded
        (path, param, value, False)
        for path, param in _DRIVE_BOOL_ROUTES
        for value in ("yeſ", "falſe")
    ]
    + [
        # the two routes `_DRIVE_BOOLS` records as declaring none
        ("/drive/v3/files/{doc}/export?mimeType=text/plain", "supportsAllDrives", "NOPE", True),
        ("/drive/v3/about?fields=user", "supportsAllDrives", "NOPE", True),
    ]
)


@pytest.mark.parametrize("path, param, value, accepted", _DRIVE_BOOL_ROWS)
def test_drive_a_declared_boolean_takes_the_protobuf_spellings_and_another_is_ignored(
    client, admin_h, path, param, value, accepted
):
    """The spellings `_DRIVE_BOOLS` records, one request per value on each route; the `files.list`
    `supportsAllDrives` rows are 24 of the 30 swept and two spellings outside ASCII. `true` is sent
    on those rows alone: four of the other flags answer a `true` with a check of their own, which
    `_DRIVE_CHECK_ROWS` holds."""
    doc = _drive_find(client, admin_h, "Brand")["id"]
    url = path.format(doc=doc)
    # the query string is built here because httpx's `params` replaces the one the row's path has
    r = client.get(f"{url}{'&' if '?' in url else '?'}{urlencode({param: value})}", headers=admin_h)
    if accepted:
        assert r.status_code == 200, r.text
        return
    field = re.sub(r"(?<!^)(?=[A-Z])", "_", param).lower()
    message = f"Invalid value at '{field}' (TYPE_BOOL), \"{value}\""
    e = _gerr(r)
    assert (e["code"], e["message"]) == (400, message)
    assert e["details"][0]["fieldViolations"] == [{"field": field, "description": message}]


_SHARED_DRIVES = (
    403,
    "supportsTeamDrivesRequired",
    None,
    "The supportsAllDrives parameter was not set to true.",
)
_ABUSE = (
    403,
    "invalidAbuseAcknowledgment",
    "acknowledgeAbuse",
    "The acknowledgeAbuse parameter is only applicable for download requests.",
)
_ADMIN_ONLY = (
    403,
    "noListTeamDrivesAdministratorPrivilege",
    None,
    "The requesting user does not have the administrator privilege required to list or manage all "
    "shared drives.",
)
_SERVED = (200, None, None, None)
_TYPED = (400, "invalid", None, None)
_RANGE = (400, "invalidParameter", "page_size", None)
_Q_REFUSED = (400, "invalid", "q", "Invalid Value")


def _mask_refused(subject: str) -> tuple:
    return (400, "invalidParameter", "fields", f"Invalid field selection {subject}")


_DOC = "/drive/v3/files/{doc}"
_PERMS_OF_DOC = "/drive/v3/files/{doc}/permissions"
# Masks real refused on a Google Workspace member's `files.get`, `files.list`, `permissions.list`,
# `drives.list` and `about`: a mask that is not one is named whole, an unknown name alone, the first
# in the mask's order.
_MASKS_REFUSED = (
    [
        (_DOC, mask, mask)
        for mask in [",", "id,", ",id", "id,,name", " ,id", "id, ", "id ,", "id, ,name", "id,name,"]
        + ["owners(emailAddress,)", "capabilities(canEdit,)", "capabilities/canEdit,", "owners("]
        + ["id(", "(id)", "id,\t", "id,\nname", "id," + chr(0xA0) + "name", "b x", "bogus,,id"]
        + ["id,,bogus", "bogus,id(", "owners(*id)", "id)", "id),name", "owners(kind)),id"]
        + ["owners(kind", "owners(kind,zzz", "id name", "id)name", "\n", chr(0x3000)]
        + ["owners(" * 1000 + "kind" + ")" * 1000 + ",", "owners(" * 1000 + "kind"]
        + ["id,b" + chr(cp) + "x" for cp in (0xAA, 0xB2, 0xB5, 0x9FA6, 0x21030)]
        + ["id,x-", "id,-", "id,:x", "id,x:", "id,x::y", "id,x:y:z", "id,:", "owners(:x)"]
    ]
    + [
        (_DOC, mask, name)
        for mask, name in [("id, bogus", "bogus"), (" bogus", "bogus"), ("bogus ", "bogus")]
        + [
            ("id,b" + chr(cp) + "x", "b" + chr(cp) + "x")
            for cp in (0xE9, 0xB7, 0x308, 0x640, 0x4E00)
        ]
        + [(f"id,{name}", name) for name in ("-x", "x--y", "x:y", "1:x", "x.", "x$y$z", "+x")]
        + [
            ("zzz,bogus", "zzz"),
            ("bogus,zzz", "bogus"),
            ("zzz,id,aaa", "zzz"),
            ("aaa,owners(zzz)", "aaa"),
        ]
    ]
    + [
        ("/drive/v3/files", mask, mask)
        for mask in ["files(id),", "files(id,)", "files(,id)", ",files(id)", "files/id,"]
        + ["files(id),,nextPageToken", "nextPageToken,", "files(*id)", "files(id", "files(id))"]
        + ["\n", chr(0x1C), chr(0x3000), "files(" + "owners(" * 1000 + "kind" + ")" * 1001 + ","]
    ]
    + [
        (path, mask.format(list=listed), subject.format(list=listed))
        for path, listed in ((_PERMS_OF_DOC, "permissions"), ("/drive/v3/drives", "drives"))
        for mask, subject in [("bogus", "bogus"), ("{list}(bogus)", "bogus"), ("files", "files")]
        + [("{list}(id),bogus", "bogus"), ("zzz,bogus", "zzz"), ("{list}(zzz),aaa", "zzz")]
        + [("aaa,{list}(zzz)", "aaa"), ("{list}(zzz,aaa)", "zzz"), ("{list}(id,)", "{list}(id,)")]
        + [("kind,", "kind,"), (",", ","), ("{list}(id),", "{list}(id),")]
        + [("bogus,{list}(id,)", "bogus,{list}(id,)"), ("{list}(id", "{list}(id"), ("\n", "\n")]
    ]
    + [
        ("/drive/v3/about", mask, subject)
        for mask, subject in [("user,", "user,"), (",", ","), ("zzz,bogus", "zzz")]
        + [("bogus,user", "bogus")]
    ]
)
_FIELDS_REQUIRED = (
    400,
    "required",
    "fields",
    "The 'fields' parameter is required for this method.",
)
_NOT_FOUND = (404, "notFound", "fileId", None)


def _unserved(name: str) -> tuple:
    message = (
        f"'{name}' is not served by Backlot: real Drive answers it, and nothing here carries it."
    )
    return (400, "invalidParameter", "fields", message)


# Masks naming a field real answered and nothing here carries, refused as the comment above
# `_DRIVE_FILE_UNSERVED` says.
_MASKS_UNSERVED = [
    (_DOC, "downloadRestrictions", "downloadRestrictions"),
    (_DOC, "id,clientEncryptionDetails", "clientEncryptionDetails"),
    (_DOC, "copyRequiresWriterPermission,viewersCanCopyContent", "copyRequiresWriterPermission"),
    ("/drive/v3/files", "files(id,writersCanShare)", "writersCanShare"),
    ("/drive/v3/files", "files/linkShareMetadata", "linkShareMetadata"),
    (_PERMS_OF_DOC, "permissions(deleted)", "deleted"),
    (_PERMS_OF_DOC, "permissions(id,photoLink)", "photoLink"),
    (_PERMS_OF_DOC, "permissions/permissionDetails", "permissionDetails"),
    (_PERMS_OF_DOC, "permissions(pendingOwner)", "pendingOwner"),
]
_FULLTEXT = "q=" + quote("fullText contains 'a'")
# Where real answered a mask ending in a comma among a route's other refusals, each sent beside it,
# and, as controls, where it answered a mask that is not one in another way or one naming a field
# the resource lacks, and which of two `fields` it read.
_MASK_ORDER = (
    [("/drive/v3/files", "files(id),", "pageSize=NOPE", _TYPED)]
    + [("/drive/v3/files", "files(id),", "pageSize=0", _RANGE)]
    + [
        ("/drive/v3/files", mask, extra, _mask_refused(mask))
        for mask in ["files(id),", "files(id),\t"]
        for extra in ["pageToken=bad", "q=bogus", "orderBy=bogus", "orderBy=name,name"]
        + ["orderBy=name,starred", f"{_FULLTEXT}&orderBy=name", "includeItemsFromAllDrives=true"]
        + ["pageSize=1&pageSize=2"]
    ]
    + [
        ("/drive/v3/files", mask, "q=bogus", _Q_REFUSED)
        for mask in ["files(id,,name)", "files(id,)", "files(", "\n", "bogus"]
    ]
    + [("/drive/v3/files", "bogus", "orderBy=bogus", (400, "invalid", "orderBy", "Invalid Value"))]
    + [
        (
            "/drive/v3/files",
            "bogus",
            "pageToken=bad",
            (400, "invalid", "pageToken", "Invalid Value"),
        )
    ]
    + [
        ("/drive/v3/files/{nope}", mask, "", _mask_refused(mask))
        for mask in ["id,", "id, ", "id,\t", "id,\n", "id," + chr(0x0B), ",", "id,,", "bogus,"]
        + ["owners(kind,"]
    ]
    + [
        ("/drive/v3/files/{nope}", mask, "", _NOT_FOUND)
        for mask in ["id,,name", "owners(", "id)", "\n", "id name", ",id", "bogus", "files(id,)"]
        + ["id," + chr(0x1C), "id," + chr(0xA0)]
    ]
    + [(_DOC, "id,", "acknowledgeAbuse=true", _mask_refused("id,"))]
    + [(_DOC, "id,", "supportsAllDrives=NOPE", _TYPED)]
    + [("/drive/v3/files/{nope}/permissions", "kind,", "", _mask_refused("kind,"))]
    + [
        ("/drive/v3/files/{nope}/permissions", mask, "", _NOT_FOUND)
        for mask in ["kind,,permissions", "permissions(", "\n"]
    ]
    + [
        (path, "kind,", extra, expected)
        for path in (_PERMS_OF_DOC, "/drive/v3/drives")
        for extra, expected in [("pageSize=NOPE", _TYPED), ("useDomainAdminAccess=NOPE", _TYPED)]
        + [("pageSize=0", _RANGE), ("pageSize=1&pageSize=2", _mask_refused("kind,"))]
        + [("useDomainAdminAccess=true", _mask_refused("kind,"))]
    ]
    + [(_PERMS_OF_DOC, "kind,", "supportsAllDrives=NOPE", _TYPED)]
    + [(_PERMS_OF_DOC, "kind,", "pageToken=bad", _mask_refused("kind,"))]
    + [
        ("/drive/v3/drives", "kind,", extra, _mask_refused("kind,"))
        for extra in ["pageToken=x", "q=bogus"]
    ]
    + [("/drive/v3/drives", mask, "q=bogus", _Q_REFUSED) for mask in ["kind,,drives", "\n"]]
    + [(_PERMS_OF_DOC, "kind,", "fields=kind", _mask_refused("kind,"))]
    + [(_PERMS_OF_DOC, "kind", "fields=" + quote("kind,"), _SERVED)]
    + [(_DOC, "id", "fields=" + quote("id,"), _SERVED)]
)
# Masks `about` answered with the missing-mask 400 `_drive_about_field_keys` describes.
_ABOUT_MASKS_REQUIRED = ["\n", chr(0x1C), chr(0x3000), "user()", "user( )", "kind()"]
# What a member's `drives.list` answered each `q` with, as `_DRIVES_Q` records.
_DRIVES_Q_TAKEN = [
    "",
    "name contains 'a'",
    "name = 'a'",
    "name != 'a'",
    "name = 'a\\'b'",
    "hidden = true",
    "hidden = TRUE",
    "hidden != true",
    "hidden",
    "not hidden",
    "not hidden = true",
    "(name = 'a')",
    "name = 'a' or name = 'b'",
    "name = 'a' or hidden = true",
    "name contains 'a' and not hidden = true",
    "name < 'a'",
    "name <= 'a'",
    "name > 'a'",
    "name >= 'a'",
    "not name < 'a'",
    "name < 'a' and hidden = false",
    "not (not hidden)",
    "not (hidden)",
    "()",
    "not ()",
    "name contains 'a' and ()",
    "hidden = true or",
    "(name contains 'a' or) and hidden = false",
    'name = "x"',
    'name contains "a"',
]
_DRIVES_Q_REFUSED = [
    " ",
    "\t",
    "bogus",
    "name",
    "name contains",
    "name = a",
    "name ~ 'a'",
    "NAME = 'a'",
    "hidden = 'yes'",
    "memberCount > 1",
    "memberCount > 'a'",
    "organizerCount = 1",
    "orgUnitId = 'x'",
    "createdTime > '2020-01-01T00:00:00'",
    "name contains 'a' or memberCount > 1",
    "mimeType = 'a'",
    "trashed = false",
    "'root' in parents",
    "fullText contains 'a'",
    "starred = true",
    "not not hidden",
    "not not name contains 'a'",
    "not(hidden)",
    "hidden" + chr(0xA0) + "= true",
    "( )",
    "()()",
    "not()",
    "hidden = true and",
]
_PAGE_EXPIRED = (403, "pageTokenExpired", None, None)
_UNKNOWN = (500, None, None, None)

# A Drive request beside real's answer, one request per row: the route, its query and who sends it,
# then the status and `errors[0]`'s reason, location and, for the three 403s a flag spelled `true`
# is refused with (`_drive_true`), message. The `1` spellings, which parse as true and run no check,
# are rows of `_DRIVE_BOOL_ROWS`.
# fmt: off
_DRIVE_CHECK_ROWS = [
    # the export and download refusals
    ("/drive/v3/files/{pdf}/export", "mimeType=text/plain", "admin", (403, "fileNotExportable", None, None)),
    # the empty value is present, so in the order `drive_files_export` records it meets the 403
    ("/drive/v3/files/{pdf}/export", "mimeType=", "admin", (403, "fileNotExportable", None, None)),
    # a folder is synthesized rather than stored, and is refused as the PDF is, not as a file that
    # does not exist
    ("/drive/v3/files/{folder}/export", "mimeType=text/plain", "admin", (403, "fileNotExportable", None, "Export only supports Docs Editors files.")),
    ("/drive/v3/files/{doc}", "alt=media", "admin", (403, "fileNotDownloadable", "alt", None)),
    # the shared-drive items need a companion flag, which the same spelling turns on
    ("/drive/v3/files", "includeItemsFromAllDrives=true", "admin", _SHARED_DRIVES),
    ("/drive/v3/files", "includeItemsFromAllDrives=TRUE", "admin", _SHARED_DRIVES),
    ("/drive/v3/files", "includeItemsFromAllDrives=tRuE", "admin", _SHARED_DRIVES),
    ("/drive/v3/files", "includeItemsFromAllDrives=t", "admin", _SERVED),
    ("/drive/v3/files", "includeItemsFromAllDrives=yes", "admin", _SERVED),
    ("/drive/v3/files", "includeTeamDriveItems=true", "admin", _SHARED_DRIVES),
    ("/drive/v3/files", "includeItemsFromAllDrives=true&supportsAllDrives=tRuE", "admin", _SERVED),
    ("/drive/v3/files", "includeItemsFromAllDrives=true&supportsAllDrives=t", "admin", _SHARED_DRIVES),
    ("/drive/v3/files", "includeItemsFromAllDrives=true&supportsAllDrives=1", "admin", _SHARED_DRIVES),
    ("/drive/v3/files", "includeItemsFromAllDrives=true&supportsAllDrives=yes", "admin", _SHARED_DRIVES),
    ("/drive/v3/files", "includeItemsFromAllDrives=true&supportsTeamDrives=true", "admin", _SERVED),
    ("/drive/v3/files", "includeTeamDriveItems=true&supportsTeamDrives=true", "admin", _SERVED),
    ("/drive/v3/files", "includeTeamDriveItems=true&supportsTeamDrives=1", "admin", _SHARED_DRIVES),
    ("/drive/v3/files", "includeTeamDriveItems=true&supportsAllDrives=true", "admin", _SERVED),
    # each read from its first repeat
    ("/drive/v3/files", "includeItemsFromAllDrives=true&includeItemsFromAllDrives=false", "admin", _SHARED_DRIVES),
    ("/drive/v3/files", "includeItemsFromAllDrives=false&includeItemsFromAllDrives=true", "admin", _SERVED),
    ("/drive/v3/files", "includeItemsFromAllDrives=true&supportsAllDrives=false&supportsAllDrives=true", "admin", _SHARED_DRIVES),
    ("/drive/v3/files", "includeItemsFromAllDrives=true&supportsAllDrives=true&supportsAllDrives=false", "admin", _SERVED),
    # its place among the other refusals, which `drive_files_list` records
    ("/drive/v3/files", "includeItemsFromAllDrives=true&supportsAllDrives=NOPE", "admin", _TYPED),
    ("/drive/v3/files", "includeItemsFromAllDrives=true&pageSize=0", "admin", _RANGE),
    ("/drive/v3/files", "includeItemsFromAllDrives=true&orderBy=bogus", "admin", (400, "invalid", "orderBy", None)),
    ("/drive/v3/files", "includeItemsFromAllDrives=true&orderBy=name,name", "admin", (403, "orderByContainsDuplicateSortKeys", "orderBy", None)),
    ("/drive/v3/files", "includeItemsFromAllDrives=true&orderBy=name,starred", "admin", _SHARED_DRIVES),
    ("/drive/v3/files", "q=fullText%20contains%20%27zzqqxx%27&orderBy=viewedByMeTime&includeItemsFromAllDrives=true", "admin", _SHARED_DRIVES),
    ("/drive/v3/files", "includeItemsFromAllDrives=true&q=bad", "admin", _SHARED_DRIVES),
    ("/drive/v3/files", "includeItemsFromAllDrives=true&pageToken=bad", "admin", _SHARED_DRIVES),
    ("/drive/v3/files", "includeItemsFromAllDrives=true&fields=bad", "admin", _SHARED_DRIVES),
    # acknowledging abuse on a read that downloads nothing
    ("/drive/v3/files/{doc}", "acknowledgeAbuse=true", "admin", _ABUSE),
    ("/drive/v3/files/{doc}", "acknowledgeAbuse=TRUE", "admin", _ABUSE),
    ("/drive/v3/files/{doc}", "acknowledgeAbuse=t", "admin", _SERVED),
    ("/drive/v3/files/{doc}", "acknowledgeAbuse=true&acknowledgeAbuse=false", "admin", _ABUSE),
    ("/drive/v3/files/{doc}", "acknowledgeAbuse=false&acknowledgeAbuse=true", "admin", _SERVED),
    ("/drive/v3/files/{pdf}", "acknowledgeAbuse=true&alt=media", "admin", _SERVED),
    ("/drive/v3/files/{pdf}", "acknowledgeAbuse=true&alt=json", "admin", _ABUSE),
    ("/drive/v3/files/{doc}", "acknowledgeAbuse=true&alt=media", "admin", (403, "fileNotDownloadable", "alt", None)),
    ("/drive/v3/files/{doc}", "acknowledgeAbuse=true&fields=bad", "admin", _ABUSE),
    ("/drive/v3/files/{doc}", "acknowledgeAbuse=true&supportsAllDrives=NOPE", "admin", _TYPED),
    # before the lookup: a file that does not exist and one the caller cannot see, beside the
    # second without the flag
    ("/drive/v3/files/{nope}", "acknowledgeAbuse=true", "admin", _ABUSE),
    ("/drive/v3/files/{hidden}", "acknowledgeAbuse=true", "mia", _ABUSE),
    ("/drive/v3/files/{hidden}", "acknowledgeAbuse=false", "mia", (404, "notFound", "fileId", None)),
    ("/drive/v3/files/{hidden}", "acknowledgeAbuse=false", "admin", _SERVED),
    # the routes that declare no such check
    ("/drive/v3/files/{doc}/export", "mimeType=text/plain&acknowledgeAbuse=true", "admin", _SERVED),
    ("/drive/v3/about", "fields=user&includeItemsFromAllDrives=true", "admin", _SERVED),
    # a domain administrator's access, which no caller here has
    ("/drive/v3/files/{doc}/permissions", "useDomainAdminAccess=true", "admin", (404, "notFound", "fileId", None)),
    ("/drive/v3/files/{doc}/permissions", "useDomainAdminAccess=TRUE", "admin", (404, "notFound", "fileId", None)),
    ("/drive/v3/files/{hidden}/permissions", "useDomainAdminAccess=true", "mia", (404, "notFound", "fileId", None)),
    ("/drive/v3/files/{hidden}/permissions", "useDomainAdminAccess=false", "mia", (404, "notFound", "fileId", None)),
    ("/drive/v3/files/{hidden}/permissions", "useDomainAdminAccess=false", "admin", _SERVED),
    ("/drive/v3/files/{doc}/permissions", "useDomainAdminAccess=true&pageSize=0", "admin", _RANGE),
    ("/drive/v3/files/{doc}/permissions", "useDomainAdminAccess=true&supportsAllDrives=NOPE", "admin", _TYPED),
    # a token `permissions.list` issued for {doc}, refused for another file once that file is found
    # and past the domain-administrator refusal, beside two `pageSize` values whose first is outside
    # 1-100, the 500 ahead of all of it
    ("/drive/v3/files/{doc}/permissions", "pageSize=1&pageToken={issued}", "admin", _SERVED),
    ("/drive/v3/files/{pdf}/permissions", "pageSize=1&pageToken={issued}", "admin", _PAGE_EXPIRED),
    ("/drive/v3/files/{pdf}/permissions", "pageToken={issued}&useDomainAdminAccess=true", "admin", (404, "notFound", "fileId", None)),
    ("/drive/v3/files/{nope}/permissions", "pageToken={issued}", "admin", (404, "notFound", "fileId", None)),
    ("/drive/v3/files/{hidden}/permissions", "pageToken={issued}", "mia", (404, "notFound", "fileId", None)),
    ("/drive/v3/files/{pdf}/permissions", "pageSize=0&pageSize=2&pageToken={issued}", "admin", _UNKNOWN),
    ("/drive/v3/files/{doc}/permissions", "pageSize=0&pageSize=2&useDomainAdminAccess=true", "admin", _UNKNOWN),
    ("/drive/v3/files/{nope}/permissions", "pageSize=0&pageSize=2", "admin", _UNKNOWN),
    ("/drive/v3/files/{hidden}/permissions", "pageSize=0&pageSize=2", "mia", _UNKNOWN),
    ("/drive/v3/drives", "useDomainAdminAccess=true", "admin", _ADMIN_ONLY),
    ("/drive/v3/drives", "useDomainAdminAccess=TRUE", "admin", _ADMIN_ONLY),
    ("/drive/v3/drives", "useDomainAdminAccess=true&useDomainAdminAccess=false", "admin", _ADMIN_ONLY),
    ("/drive/v3/drives", "useDomainAdminAccess=false&useDomainAdminAccess=true", "admin", _SERVED),
    ("/drive/v3/drives", "useDomainAdminAccess=true&pageSize=0", "admin", _RANGE),
    ("/drive/v3/drives", "useDomainAdminAccess=true&q=name%3D%27x%27", "admin", _ADMIN_ONLY),
    # the `q` a member's `drives.list` takes, and where its refusal falls among the others
    *[("/drive/v3/drives", f"q={quote(q)}", "admin", _SERVED) for q in _DRIVES_Q_TAKEN],
    *[("/drive/v3/drives", f"q={quote(q)}", "admin", _Q_REFUSED) for q in _DRIVES_Q_REFUSED],
    ("/drive/v3/drives", "q=bogus&q=name%3D%27a%27", "admin", _Q_REFUSED),
    ("/drive/v3/drives", "q=name%3D%27a%27&q=bogus", "admin", _SERVED),
    ("/drive/v3/drives", "q=bogus&pageSize=NOPE", "admin", _TYPED),
    ("/drive/v3/drives", "pageSize=NOPE&q=bogus", "admin", _TYPED),
    ("/drive/v3/drives", "q=bogus&useDomainAdminAccess=NOPE", "admin", _TYPED),
    ("/drive/v3/drives", "q=bogus&pageSize=0", "admin", _RANGE),
    ("/drive/v3/drives", "q=bogus&pageToken=x", "admin", _Q_REFUSED),
    ("/drive/v3/drives", "pageToken=x&q=bogus", "admin", _Q_REFUSED),
    ("/drive/v3/drives", "q=bogus&useDomainAdminAccess=true", "admin", _Q_REFUSED),
    ("/drive/v3/drives", "useDomainAdminAccess=true&q=bogus", "admin", _Q_REFUSED),
    # a mask, and where its refusal falls among the others
    *[(path, f"fields={quote(mask)}", "admin", _mask_refused(subject)) for path, mask, subject in _MASKS_REFUSED],
    *[("/drive/v3/about", f"fields={quote(mask)}", "admin", _FIELDS_REQUIRED) for mask in _ABOUT_MASKS_REQUIRED],
    *[(path, f"fields={quote(mask)}" + (extra and f"&{extra}"), "admin", expected) for path, mask, extra, expected in _MASK_ORDER],
    *[(path, f"fields={quote(mask)}", "admin", _unserved(name)) for path, mask, name in _MASKS_UNSERVED],
    ("/drive/v3/files/{pdf}/permissions", "fields=bogus&pageToken={issued}", "admin", _PAGE_EXPIRED),
    ("/drive/v3/files/{pdf}/permissions", "fields=kind%2C&pageToken={issued}", "admin", _mask_refused("kind,")),
    *[(path, "fields=bogus&fields=kind", "admin", _mask_refused("bogus")) for path in (_PERMS_OF_DOC, "/drive/v3/drives")],
    *[(path, "fields=kind&fields=bogus", "admin", _SERVED) for path in (_PERMS_OF_DOC, "/drive/v3/drives")],
    *[(path, f"fields=bogus&{query}", "admin", _TYPED) for path in (_PERMS_OF_DOC, "/drive/v3/drives") for query in ("pageSize=NOPE", "useDomainAdminAccess=NOPE")],
    *[(path, "fields=bogus&pageSize=0", "admin", _RANGE) for path in (_PERMS_OF_DOC, "/drive/v3/drives")],
    (_PERMS_OF_DOC, "fields=bogus&pageToken=bad", "admin", (400, "invalid", "pageToken", "Invalid Value")),
    (_PERMS_OF_DOC, "pageToken=bad&fields=bogus", "admin", (400, "invalid", "pageToken", "Invalid Value")),
    (_PERMS_OF_DOC, "fields=bogus&useDomainAdminAccess=true", "admin", (404, "notFound", "fileId", None)),
    ("/drive/v3/files/{nope}/permissions", "fields=bogus", "admin", (404, "notFound", "fileId", None)),
    ("/drive/v3/drives", "fields=bogus&pageToken=x", "admin", (400, "invalid", "pageToken", "Invalid Value")),
    ("/drive/v3/drives", "fields=bogus&useDomainAdminAccess=true", "admin", _ADMIN_ONLY),
    ("/drive/v3/drives", "fields=bogus&q=bogus", "admin", _Q_REFUSED),
]
# fmt: on


@pytest.mark.parametrize("path, query, caller, expected", _DRIVE_CHECK_ROWS)
def test_drive_answers_each_check_with_reals_status_and_reason(
    client, admin_h, tokens, path, query, caller, expected
):
    """The rows `_DRIVE_CHECK_ROWS` records. The 403 for a file the scoped token cannot see is the
    one for a file that does not exist, and both name no file, so the check tells the caller
    nothing about what it cannot read; the permissions 404 names the file it was asked about, and
    is the same 404 the scoped token gets for that file without the flag."""
    doc = _drive_find(client, admin_h, "Brand")["id"]
    issued = client.get(
        f"/drive/v3/files/{doc}/permissions", headers=admin_h, params={"pageSize": 1}
    )
    ids = {
        "doc": doc,
        "pdf": _drive_find(client, admin_h, "Whitepaper")["id"],
        "folder": _drive_ids(client, admin_h, q=f"mimeType='{FOLDER_MIME}'")[0],
        "hidden": _drive_find(client, admin_h, "Q1 Revenue Model")["id"],
        "nope": "nosuchfile000",
        "issued": issued.json()["nextPageToken"],
    }
    headers = (
        admin_h if caller == "admin" else {"Authorization": f"Bearer {tokens['mia@acme.com']}"}
    )
    r = client.get(f"{path.format(**ids)}?{query.format(**ids)}", headers=headers)
    status, reason, location, message = expected
    assert r.status_code == status, r.text
    if status == 200:
        return
    e = _gerr(r)
    assert (e["errors"][0].get("reason"), e["errors"][0].get("location")) == (reason, location)
    if message is not None:
        assert (e["message"], "status" in e) == (message, False)


def test_reads_of_a_folder_the_caller_cannot_see(tmp_path):
    """The answers `files.export`, `files.get` and `permissions.list` record for a folder, for each
    caller, and the ones Docs and Sheets record for mia: `Private` holds only a file mia cannot
    read, so it is the folder she cannot see."""
    s = tiny_corpus(
        tmp_path,
        [
            {
                "source_type": "google_drive",
                "doc_id": "gd-open",
                "folder": "Shared",
                "title": "Open",
                "content": "body",
                "author_email": "mia@acme.com",
                "visibility": "public",
            },
            {
                "source_type": "google_drive",
                "doc_id": "gd-shut",
                "folder": "Private",
                "title": "Shut",
                "content": "body",
                "author_email": "ava@acme.com",
                "readers": ["ava@acme.com"],
            },
        ],
    )
    tokens = yaml.safe_load(s.tokens_path.read_text(encoding="utf-8"))
    h = {"admin": {"Authorization": f"Bearer {s.admin_token}"}}
    for name in ("ava", "mia"):
        h[name] = {"Authorization": f"Bearer {tok(tokens, f'{name}@acme.com')}"}
    with client_for(s, reload=True) as c:
        q = {"q": f"mimeType='{FOLDER_MIME}'", "fields": "files(id,name)"}
        listed = c.get("/drive/v3/files", headers=h["admin"], params=q).json()["files"]
        folder = {f["name"]: f["id"] for f in listed}
        folder["nope"] = "nosuchfile000"
        for caller, name, status, reason in [
            ("admin", "Private", 403, "fileNotExportable"),
            ("ava", "Private", 403, "fileNotExportable"),
            ("mia", "Private", 404, "notFound"),
            ("mia", "Shared", 403, "fileNotExportable"),
            ("mia", "nope", 404, "notFound"),
        ]:
            r = c.get(
                f"/drive/v3/files/{folder[name]}/export",
                headers=h[caller],
                params={"mimeType": "text/plain"},
            )
            assert (r.status_code, _gerr(r)["errors"][0]["reason"]) == (status, reason), (
                caller,
                name,
            )
        files, perms = "/drive/v3/files/{}", "/drive/v3/files/{}/permissions"
        docs, sheets = "/docs/v1/documents/{}", "/sheets/v4/spreadsheets/{}"
        for route, query, caller, name, status, answer in [
            (files, {}, "admin", "Private", 200, None),
            (files, {}, "ava", "Private", 200, None),
            (files, {}, "mia", "Private", 404, "notFound"),
            (files, {}, "mia", "Shared", 200, None),
            (files, {}, "mia", "nope", 404, "notFound"),
            (files, {"fields": "totallyBogusField"}, "mia", "Private", 404, "notFound"),
            (files, {"fields": "totallyBogusField"}, "mia", "Shared", 400, "invalidParameter"),
            (files, {"alt": "media"}, "mia", "Private", 404, "notFound"),
            (files, {"alt": "media"}, "mia", "Shared", 403, "fileNotDownloadable"),
            (files, {"alt": "MEDIA"}, "mia", "Shared", 403, "fileNotDownloadable"),
            (files, {"alt": "media"}, "admin", "Private", 403, "fileNotDownloadable"),
            (perms, {}, "admin", "Private", 200, None),
            (perms, {}, "ava", "Private", 200, None),
            (perms, {}, "mia", "Private", 404, "notFound"),
            (perms, {}, "mia", "Shared", 200, None),
            (perms, {}, "mia", "nope", 404, "notFound"),
            (perms, {"pageSize": "0"}, "mia", "Private", 400, "invalidParameter"),
            (perms, {"pageToken": "bad"}, "mia", "Private", 400, "invalid"),
            (docs, {}, "mia", "Private", 400, "INVALID_ARGUMENT"),
            (docs, {}, "mia", "nope", 404, "NOT_FOUND"),
            (sheets, {}, "mia", "Private", 400, "INVALID_ARGUMENT"),
            (sheets, {}, "mia", "nope", 404, "NOT_FOUND"),
        ]:
            r = c.get(route.format(folder[name]), headers=h[caller], params=query)
            error = r.json().get("error", {})
            got = error.get("status") or (error.get("errors") or [{}])[0].get("reason")
            assert (r.status_code, got) == (status, answer), (route, query, caller, name)


@pytest.mark.parametrize(
    "path, query, body, good",
    [
        ("/drive/v3/files/{id}", "acknowledgeAbuse=NOPE", None, None),
        ("/drive/v3/files/{id}/permissions", "supportsAllDrives=NOPE", None, None),
        ("/drive/v3/files/{id}/permissions", "pageSize=0", None, None),
        ("/drive/v3/files/{id}/permissions", "pageToken=bad", None, None),
        ("/sheets/v4/spreadsheets/{id}/values/Sheet1!A1", "majorDimension=NOPE", None, None),
        ("/sheets/v4/spreadsheets/{id}/values:batchGet", "valueRenderOption=NOPE", None, None),
        ("/sheets/v4/spreadsheets/{id}", "includeGridData=NOPE", None, None),
        # the data-filter reads, whose typed values are in the body, served the empty grid range
        # they answer at 200
        (
            "/sheets/v4/spreadsheets/{id}/values:batchGetByDataFilter",
            "",
            {"dataFilters": [{"gridRange": {"startRowIndex": "abc"}}]},
            {"dataFilters": [{"gridRange": {"startRowIndex": 1, "endRowIndex": 1}}]},
        ),
        (
            "/sheets/v4/spreadsheets/{id}:getByDataFilter",
            "",
            {"dataFilters": [{"gridRange": {"startRowIndex": "abc"}}]},
            {"dataFilters": [{"gridRange": {"startRowIndex": 1, "endRowIndex": 1}}]},
        ),
        # and a developer metadata lookup, which selects nothing
        (
            "/sheets/v4/spreadsheets/{id}/values:batchGetByDataFilter",
            "",
            {"dataFilters": [{"developerMetadataLookup": {"locationType": "NOPE"}}]},
            {"dataFilters": [{"developerMetadataLookup": {"metadataKey": "owner"}}]},
        ),
        (
            "/sheets/v4/spreadsheets/{id}:getByDataFilter",
            "",
            {"dataFilters": [{"developerMetadataLookup": {"locationType": "NOPE"}}]},
            {"dataFilters": [{"developerMetadataLookup": {"metadataKey": "owner"}}]},
        ),
    ],
)
def test_a_typed_refusal_comes_after_the_credential_and_before_the_lookup(
    client, admin_h, tokens, path, query, body, good
):
    """The order `_typed_query`'s docstring records, and `sheets_values_batch_get_by_data_filter`'s
    for a data-filter body. The refusal is the same bytes for a spreadsheet the scoped token cannot
    see as for one that does not exist. Without the bad value (with ``good`` for a body) the hidden
    spreadsheet is a 200 to the admin. To the scoped token a Sheets read answers the hidden
    spreadsheet 403 and the missing id 404, where Drive answers both 404. With no credential or a
    bad one the bad value changes nothing: the answer is the credential's refusal, the 401 for a bad
    one."""

    def send(url, headers, bad=False):
        if body is None:
            return client.get(f"{url}?{query}" if bad else url, headers=headers)
        return client.post(url, headers=headers, json=body if bad else good)

    scoped = {"Authorization": f"Bearer {tokens['mia@acme.com']}"}
    missing = path.format(id="nosuchspreadsheet000")
    hidden = path.format(id=_drive_find(client, admin_h, "Q1 Revenue Model")["id"])
    refused = send(missing, scoped, bad=True)
    assert _gerr(refused)["code"] == 400
    assert send(hidden, scoped, bad=True).content == refused.content
    assert send(hidden, admin_h).status_code == 200
    expected_hidden = 403 if path.startswith("/sheets/") else 404
    assert send(hidden, scoped).status_code == expected_hidden
    assert send(missing, scoped).status_code == 404
    assert send(missing, BAD_TOKEN).status_code == 401
    for headers in ({}, BAD_TOKEN):
        assert send(missing, headers, bad=True).content == send(missing, headers).content


@pytest.mark.parametrize("mask", ["", " ", "\t"])
def test_drive_a_blank_fields_mask_selects_nothing(client, admin_h, mask):
    """The blank mask `_drive_get_field_keys` describes, on a listing, a file and a folder, and
    `about`'s missing-mask 400 for the same mask. An absent mask is the default object."""
    doc = _drive_find(client, admin_h, "Brand")["id"]
    folder = client.get(
        "/drive/v3/files",
        headers=admin_h,
        params={"q": "mimeType='application/vnd.google-apps.folder'"},
    ).json()["files"][0]["id"]
    for path in ("/drive/v3/files", f"/drive/v3/files/{doc}", f"/drive/v3/files/{folder}"):
        assert client.get(path, headers=admin_h, params={"fields": mask}).json() == {}, path
        assert client.get(path, headers=admin_h).json(), path
    about = _gerr(client.get(ABOUT, headers=admin_h, params={"fields": mask}))
    assert about["message"] == "The 'fields' parameter is required for this method."


# What real answered each mask with. The third column is the top-level keys, `None` where the
# answer was the unmasked one. The fourth is each entry's keys on a listing: `"default"` where each
# entry was the unmasked one, an empty set where the list came back empty because every entry was
# left with no key, and `None` where the entries are not compared. A documented field the file has
# no value for leaves it out of the answer here, where real answers it.
_MASKS_SERVED = [
    (_DOC, "id , name", {"id", "name"}, None),
    (_DOC, "id,\tname", {"id", "name"}, None),
    (_DOC, "id\t,name", {"id", "name"}, None),
    (_DOC, "  id  ", {"id"}, None),
    (_DOC, "owners()", set(), None),
    (_DOC, "owners( )", set(), None),
    (_DOC, "owners (kind),id", {"owners", "id"}, None),
    (_DOC, "owners/ kind,id", {"owners", "id"}, None),
    (_DOC, "owners /kind,id", {"owners", "id"}, None),
    (_DOC, "owners(emailAddress , displayName),id", {"owners", "id"}, None),
    (_DOC, "owners(),id", {"id"}, None),
    (_DOC, "*,id", None, None),
    (_DOC, "id,*", None, None),
    (_DOC, "id()", {"id"}, None),
    (_DOC, "id( )", {"id"}, None),
    (_DOC, "name()", {"name"}, None),
    (_DOC, "kind()", {"kind"}, None),
    (_DOC, "mimeType()", {"mimeType"}, None),
    (_DOC, "kind(),id", {"kind", "id"}, None),
    (_DOC, "createdTime()", set(), None),
    (_DOC, "trashed()", set(), None),
    ("/drive/v3/files", "*", {"files"}, "default"),
    ("/drive/v3/files", "*,files(id)", {"files"}, "default"),
    ("/drive/v3/files", "files(id,*)", {"files"}, "default"),
    ("/drive/v3/files", "files()", {"files"}, set()),
    ("/drive/v3/files", "files( )", {"files"}, set()),
    ("/drive/v3/files", "files(trashingUser)", {"files"}, set()),
    ("/drive/v3/files", "files/trashingUser", {"files"}, set()),
    ("/drive/v3/files", "files(id,trashingUser)", {"files"}, {"id"}),
    ("/drive/v3/files", "files(id())", {"files"}, {"id"}),
    ("/drive/v3/files", "files(name())", {"files"}, {"name"}),
    ("/drive/v3/files", "files(createdTime())", {"files"}, set()),
    (_PERMS_OF_DOC, "", set(), None),
    (_PERMS_OF_DOC, " ", set(), None),
    (_PERMS_OF_DOC, "nextPageToken", set(), None),
    (_PERMS_OF_DOC, "kind", {"kind"}, None),
    (_PERMS_OF_DOC, " kind", {"kind"}, None),
    (_PERMS_OF_DOC, "kind,nextPageToken", {"kind"}, None),
    (_PERMS_OF_DOC, "permissions(id)", {"permissions"}, {"id"}),
    (_PERMS_OF_DOC, "permissions( id )", {"permissions"}, {"id"}),
    (_PERMS_OF_DOC, "permissions/id", {"permissions"}, {"id"}),
    (_PERMS_OF_DOC, "permissions/kind", {"permissions"}, {"kind"}),
    (_PERMS_OF_DOC, "permissions(id,role)", {"permissions"}, {"id", "role"}),
    (_PERMS_OF_DOC, "permissions(id,\trole)", {"permissions"}, {"id", "role"}),
    (_PERMS_OF_DOC, "kind ,permissions(id)", {"kind", "permissions"}, {"id"}),
    (_PERMS_OF_DOC, "permissions()", {"permissions"}, set()),
    (_PERMS_OF_DOC, "kind()", {"kind"}, None),
    (_PERMS_OF_DOC, "permissions(id())", {"permissions"}, {"id"}),
    (_PERMS_OF_DOC, "permissions(role())", {"permissions"}, {"role"}),
    (_PERMS_OF_DOC, "permissions(type())", {"permissions"}, {"type"}),
    (_PERMS_OF_DOC, "permissions(kind())", {"permissions"}, {"kind"}),
    (_PERMS_OF_DOC + "?pageSize=1", "nextPageToken()", {"nextPageToken"}, None),
    (_PERMS_OF_DOC, "permissions(emailAddress())", {"permissions"}, set()),
    (_PERMS_OF_DOC, "permissions( )", {"permissions"}, set()),
    (_PERMS_OF_DOC, "permissions(teamDrivePermissionDetails)", {"permissions"}, set()),
    (_PERMS_OF_DOC, "permissions", {"permissions"}, "default"),
    (_PERMS_OF_DOC, "permissions(*)", {"permissions"}, "default"),
    (_PERMS_OF_DOC, "*", {"kind", "permissions"}, "default"),
    (_PERMS_OF_DOC, "*,permissions(id)", {"kind", "permissions"}, "default"),
    (_PERMS_OF_DOC, "permissions(id,*)", {"permissions"}, "default"),
    ("/drive/v3/drives", "", set(), None),
    ("/drive/v3/drives", " ", set(), None),
    ("/drive/v3/drives", "nextPageToken", set(), None),
    ("/drive/v3/drives", "kind", {"kind"}, None),
    ("/drive/v3/drives", "kind,nextPageToken", {"kind"}, None),
    ("/drive/v3/drives", "drives(id)", {"drives"}, None),
    ("/drive/v3/drives", "drives/id", {"drives"}, None),
    ("/drive/v3/drives", "drives()", {"drives"}, None),
    ("/drive/v3/drives", "drives(id,name)", {"drives"}, None),
    ("/drive/v3/drives", "*", {"kind", "drives"}, None),
    ("/drive/v3/drives", "kind()", {"kind"}, None),
    ("/drive/v3/about", "user, storageQuota", {"user", "storageQuota"}, None),
    ("/drive/v3/about", "user ,kind", {"user", "kind"}, None),
    ("/drive/v3/about", "user", {"user"}, None),
    ("/drive/v3/about", "user,storageQuota", {"user", "storageQuota"}, None),
    ("/drive/v3/about", "user(kind)", {"user"}, None),
    ("/drive/v3/about", "user(*)", {"user"}, None),
]


@pytest.mark.parametrize("path, mask, top, entries", _MASKS_SERVED)
def test_drive_answers_a_fields_mask_with_what_it_selects(
    client, admin_h, path, mask, top, entries
):
    """The rows `_MASKS_SERVED` records. `files.list`'s own top-level keys are left whole, as
    `_drive_file_field_keys` says, so only its `files` is compared; `"default"` is each entry as an
    unmasked request gets it. A `drives.list` row compares the top-level keys alone: Backlot serves
    no shared drive, where real listed the member's two."""
    doc = _drive_find(client, admin_h, "Brand")["id"]
    url, _, query = path.format(doc=doc).partition("?")

    def get(**params):
        r = client.get(
            url, headers=admin_h, params={"pageSize": 2, **dict(parse_qsl(query)), **params}
        )
        assert r.status_code == 200, r.text
        body = r.json()
        return {"files": body["files"]} if path == "/drive/v3/files" else body

    body = get(fields=mask)
    if top is None:
        assert body == get()
        return
    assert set(body) == top
    listed = next((k for k in body if isinstance(body[k], list)), None)
    if entries == "default":
        assert body[listed] == get()[listed]
    elif entries:
        assert body[listed] and all(set(e) == entries for e in body[listed])
    elif entries is not None:
        assert body[listed] == []


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


# --- `$.xgafv`, the system parameter that selects the error envelope --------------------------

XGAFV_REFUSAL = "Invalid query parameters. Invalid value '{}' for system query parameter : $.xgafv"


@pytest.mark.parametrize("value", ["0", "3", "NOPE", "01", ""])
def test_xgafv_takes_1_or_2_and_refuses_the_rest_with_reals_sentence(client, admin_h, value):
    """Sheets carries no `errors[]` on its own refusal: the value that would have asked for one
    was the value refused."""
    r = client.get("/sheets/v4/spreadsheets/x", headers=admin_h, params={"$.xgafv": value})
    assert r.status_code == 400, r.text
    e = _gerr(r)
    assert e == {"code": 400, "message": XGAFV_REFUSAL.format(value), "status": "INVALID_ARGUMENT"}
    r = client.get("/drive/v3/files", headers=admin_h, params={"$.xgafv": value})
    e = _gerr(r)
    assert e["message"] == XGAFV_REFUSAL.format(value) and e["status"] == "INVALID_ARGUMENT"
    assert e["errors"] == [
        {"message": XGAFV_REFUSAL.format(value), "domain": "global", "reason": "badRequest"}
    ]


def test_a_folder_says_it_can_be_downloaded(tmp_path):
    """Both kinds of folder, a `subtype: folder` record and the folder a record's `folder` names,
    answer `canDownload` true, though `alt=media` refuses either. Real's did: on 2026-10-18 each of
    the 49 folders a Google Workspace member's `files.list` named (two its own, 23 shared with it,
    24 in shared drives) answered `capabilities.canDownload: true`."""
    s = tiny_corpus(
        tmp_path,
        [
            {
                "source_type": "google_drive",
                "doc_id": "gd-open",
                "folder": "Shared",
                "title": "Open",
                "content": "body",
                "author_email": "mia@acme.com",
                "visibility": "public",
            },
            {
                "source_type": "google_drive",
                "doc_id": "gd-dir",
                "subtype": "folder",
                "title": "Plans",
                "content": "x",
                "author_email": "mia@acme.com",
                "visibility": "public",
            },
        ],
    )
    h = {"Authorization": f"Bearer {s.admin_token}"}
    with client_for(s, reload=True) as c:
        q = {"q": f"mimeType='{FOLDER_MIME}'", "fields": "files(id,name)"}
        folder = {
            f["name"]: f["id"]
            for f in c.get("/drive/v3/files", headers=h, params=q).json()["files"]
        }
        for name in ("Shared", "Plans"):
            r = c.get(
                f"/drive/v3/files/{folder[name]}", headers=h, params={"fields": "capabilities"}
            )
            assert r.json()["capabilities"]["canDownload"] is True, name


def test_a_bad_xgafv_is_refused_before_anything_else_is_read(client, admin_h):
    """Measured: real answers the system-parameter 400 ahead of a bad token, a missing credential
    and an unparseable range. A router-level dependency is what puts the check first here."""
    for headers, path in (
        (BAD_TOKEN, "/sheets/v4/spreadsheets/x"),
        ({}, "/sheets/v4/spreadsheets/x"),
        (admin_h, "/sheets/v4/spreadsheets/x/values/NOPE!A1"),
        ({}, "/docs/v1/documents/x"),
    ):
        r = client.get(path, headers=headers, params={"$.xgafv": "3"})
        assert r.status_code == 400, (path, r.text)
        assert _gerr(r)["message"] == XGAFV_REFUSAL.format("3")


def test_xgafv_1_puts_the_errors_array_on_an_editor_error_and_2_does_not(client, admin_h):
    """The same 404, three ways. `2` and an absent value are the envelope Backlot always served;
    `1` adds `errors[]`, and the LAST repeat wins when the parameter is sent twice — measured,
    `2&1` carries the array and `1&2` does not."""
    doc = _drive_find(client, admin_h, "Brand")["id"]
    path = f"/sheets/v4/spreadsheets/{doc}"
    plain = _gerr(client.get(path, headers=admin_h))
    assert plain["code"] == 404 and "errors" not in plain
    assert _gerr(client.get(path, headers=admin_h, params={"$.xgafv": "2"})) == plain
    v1 = _gerr(client.get(path, headers=admin_h, params={"$.xgafv": "1"}))
    assert v1 == {
        **plain,
        "errors": [
            {"message": "Requested entity was not found.", "domain": "global", "reason": "notFound"}
        ],
    }
    assert _gerr(client.get(f"{path}?$.xgafv=1&$.xgafv=2", headers=admin_h)) == plain
    assert _gerr(client.get(f"{path}?$.xgafv=2&$.xgafv=1", headers=admin_h)) == v1


def test_xgafv_changes_nothing_about_a_success_or_a_drive_error(client, admin_h):
    """Measured: a 200 body is byte-identical under `1`, `2` and no parameter, and Drive's envelope
    — `errors[]` on every error already — reads the same under all three."""
    doc = _drive_find(client, admin_h, "Brand")["id"]
    bodies = [
        client.get(f"/drive/v3/files/{doc}", headers=admin_h, params=p).json()
        for p in ({}, {"$.xgafv": "1"}, {"$.xgafv": "2"})
    ]
    assert bodies[0] == bodies[1] == bodies[2]
    errors = [
        _gerr(client.get("/drive/v3/files", headers=admin_h, params={"fields": "nope", **p}))
        for p in ({}, {"$.xgafv": "1"}, {"$.xgafv": "2"})
    ]
    assert errors[0] == errors[1] == errors[2]
    assert errors[0]["errors"][0]["reason"] == "invalidParameter"


def test_gmail_carries_the_errors_array_unless_xgafv_is_2(client):
    """Gmail is the family that opts OUT, where the editor families opt in and Drive never does.

    Measured on the one Gmail error a request with no scope can reach, its anonymous 401, over both
    `users/me/labels` and `users/me/messages`: the array is there with no parameter and at `1`, and
    gone at `2`. The value read is the last repeat, and it is that value the rule tests — `2&0` is
    a refusal, not a `2`, and keeps the array; `0&2` drops it."""
    for params, carried in (({}, True), ({"$.xgafv": "1"}, True), ({"$.xgafv": "2"}, False)):
        for path in ("/gmail/v1/users/me/labels", "/gmail/v1/users/me/messages"):
            e = _gerr(client.get(path, params=params))
            assert e["code"] == 401, (path, params)
            assert ("errors" in e) is carried, (path, params)
            if carried:
                assert e["errors"][0]["reason"] == "required", (path, params)
    repeated = {
        q: _gerr(client.get(f"/gmail/v1/users/me/labels?{q}"))
        for q in ("$.xgafv=2&$.xgafv=0", "$.xgafv=0&$.xgafv=2")
    }
    assert repeated["$.xgafv=2&$.xgafv=0"]["errors"][0]["reason"] == "badRequest"
    assert "errors" not in repeated["$.xgafv=0&$.xgafv=2"]


# (what is sent, the `errors[0]` real answered at `$.xgafv=1`) — the entry is not uniform, and
# which constructor raised the error decides its shape. `{path}` is the SAMPLE spreadsheet's id.
XGAFV_ENTRIES = [
    # a typed value the proto layer refuses: `invalid`, and no `domain`
    (
        "/sheets/v4/spreadsheets/{sid}/values/Sheet1!A1",
        {"majorDimension": "NOPE"},
        {
            "message": "Invalid value at 'major_dimension' "
            '(type.googleapis.com/google.apps.sheets.v4.Dimension), "NOPE"',
            "reason": "invalid",
        },
    ),
    (
        "/sheets/v4/spreadsheets/{sid}",
        {"includeGridData": "maybe"},
        {
            "message": "Invalid value at 'include_grid_data' (TYPE_BOOL), \"maybe\"",
            "reason": "invalid",
        },
    ),
    # everything else the editor APIs refuse with a 400: `badRequest` under `global`
    (
        "/sheets/v4/spreadsheets/{sid}/values/NOPE!A1",
        {},
        {"message": "Unable to parse range: NOPE!A1", "domain": "global", "reason": "badRequest"},
    ),
    (
        "/sheets/v4/spreadsheets/{sid}/values/A1001",
        {},
        {
            "message": "Range (Sheet1!A1001) exceeds grid limits. Max rows: 1000, max columns: 26",
            "domain": "global",
            "reason": "badRequest",
        },
    ),
    (
        "/sheets/v4/spreadsheets/{sid}",
        {"fields": "nope"},
        {
            "message": "Request contains an invalid argument.",
            "domain": "global",
            "reason": "badRequest",
        },
    ),
    (
        "/sheets/v4/spreadsheets/{sid}/values/Sheet1!A1",
        {"alt": "media"},
        {
            "message": 'Unsupported alt type "media" for non byte stream request.',
            "domain": "global",
            "reason": "badRequest",
        },
    ),
    # not found, and the credential failures
    (
        "/sheets/v4/spreadsheets/nonexistent",
        {},
        {"message": "Requested entity was not found.", "domain": "global", "reason": "notFound"},
    ),
]


@pytest.mark.parametrize("path, params, entry", XGAFV_ENTRIES)
def test_the_errors_entry_at_xgafv_1_is_the_one_real_answers(client, admin_h, path, params, entry):
    sid = _drive_find(client, admin_h, "Q1 Revenue Model")["id"]
    r = client.get(path.format(sid=sid), headers=admin_h, params={**params, "$.xgafv": "1"})
    e = _gerr(r)
    assert e["errors"] == [entry], r.text
    assert e["message"] == entry["message"]


_A1_FILTER = {"dataFilters": [{"a1Range": "Sheet1!A1"}]}
_SHEETS_FILTER_READ = "/sheets/v4/spreadsheets/x/values:batchGetByDataFilter"


def _anonymous(client, method, path, params):
    return client.request(
        method, path, params=params, json=_A1_FILTER if method == "POST" else None
    )


def test_the_credential_entries(client):
    """The three credential failures, which differ from each other rather than by family: a bad
    token, an unregistered caller (here an anonymous Sheets GET), and the missing credential, which
    is what an anonymous request gets on an operation that takes no API key.

    The last of those is asked of Gmail with NO parameter, because Gmail carries the array by
    default, and of Docs, Slides and the Sheets data-filter POST at `$.xgafv=1`, because they do
    not. Real answers all four the same entry — measured on Gmail, Docs and Slides 2026-09-14 and
    on the Sheets POST 2026-09-22; a request with no Authorization header needs no credential to
    send."""
    e = _gerr(client.get("/sheets/v4/spreadsheets/x", headers=BAD_TOKEN, params={"$.xgafv": "1"}))
    assert e["errors"] == [
        {
            "message": "Invalid Credentials",
            "domain": "global",
            "reason": "authError",
            "location": "Authorization",
            "locationType": "header",
        }
    ]
    e = _gerr(client.get("/sheets/v4/spreadsheets/x", params={"$.xgafv": "1"}))
    assert e["code"] == 403
    assert e["errors"] == [
        {"message": e["message"], "domain": "global", "reason": "forbidden"},
    ]
    entry = {
        "message": "Login Required.",
        "domain": "global",
        "reason": "required",
        "location": "Authorization",
        "locationType": "header",
    }
    for method, path, params in (
        ("GET", "/docs/v1/documents/x", {"$.xgafv": "1"}),
        ("GET", "/slides/v1/presentations/x", {"$.xgafv": "1"}),
        ("POST", _SHEETS_FILTER_READ, {"$.xgafv": "1"}),
        ("GET", "/gmail/v1/users/me/labels", {}),
    ):
        e = _gerr(_anonymous(client, method, path, params))
        assert e["code"] == 401, path
        assert e["errors"] == [entry], path
    # The array is the only member the parameter moves: on the editor families it is what `1` adds,
    # and on Gmail, which carries it already, `1` adds nothing at all.
    for method, path in (
        ("GET", "/docs/v1/documents/x"),
        ("GET", "/slides/v1/presentations/x"),
        ("POST", _SHEETS_FILTER_READ),
    ):
        bare = _gerr(_anonymous(client, method, path, {}))
        assert "errors" not in bare, path
        verbose = _gerr(_anonymous(client, method, path, {"$.xgafv": "1"}))
        assert verbose == {**bare, "errors": [entry]}, path
    gmail = _gerr(client.get("/gmail/v1/users/me/labels"))
    assert _gerr(client.get("/gmail/v1/users/me/labels", params={"$.xgafv": "1"})) == gmail


# The system parameters every Google family operation declares, as real's discovery documents do.
_SYSTEM_PARAMETERS = [
    {
        "name": "$.xgafv",
        "in": "query",
        "required": False,
        "description": "V1 error format.",
        "schema": {"type": "string", "enum": ["1", "2"]},
    },
    {
        "name": "access_token",
        "in": "query",
        "required": False,
        "description": "OAuth access token.",
        "schema": {"type": "string"},
    },
    {
        "name": "oauth_token",
        "in": "query",
        "required": False,
        "description": "OAuth 2.0 token for the current user.",
        "schema": {"type": "string"},
    },
]


def test_every_google_operation_declares_the_system_parameters_and_reads_them(client):
    """Both directions, and the document asked for twice.

    Declared: every family operation carries `$.xgafv`, `access_token` and `oauth_token`, `/batch`
    none of them. Read: each operation is SENT a `$.xgafv` the parameter does not take and has to
    refuse it, and an `access_token` of Google's shape that does not resolve and has to answer the
    bad token's 401 — the declaration is derived from the path, so it would appear on a family
    route served by some other router while the code that reads it never ran, a gap only this half
    sees. The path parameters are dummies; both answers come before the route's own work. The
    second fetch holds the enrichment idempotent, since it edits the schema FastAPI caches and hands
    back by identity."""
    client.get("/openapi.json")
    spec = client.get("/openapi.json").json()
    families = ("/drive/v3", "/gmail/v1", "/docs/v1", "/sheets/v4", "/slides/v1")
    names = {p["name"] for p in _SYSTEM_PARAMETERS}
    missing, batch, unchecked = [], [], []
    for path, item in spec["paths"].items():
        for method, op in item.items():
            if method not in ("get", "post", "put", "patch", "delete"):
                continue
            declared = [p for p in op.get("parameters", []) if p["name"] in names]
            if path.startswith(families):
                if declared != _SYSTEM_PARAMETERS:
                    missing.append(f"{method.upper()} {path}")
                url = re.sub(r"\{[^}]+\}", "dummy", path)
                r = client.request(method.upper(), f"{url}?$.xgafv=0", json={})
                if r.status_code != 400 or "system query parameter" not in r.text:
                    unchecked.append(f"{method.upper()} {path} -> {r.status_code}")
                r = client.request(method.upper(), f"{url}?access_token=ya29.x", json={})
                if r.headers.get("www-authenticate") != _CHALLENGED:
                    unchecked.append(f"{method.upper()} {path} access_token -> {r.status_code}")
            elif path.startswith("/batch") and declared:
                batch.append(f"{method.upper()} {path}")
    assert missing == [] and batch == [] and unchecked == []


def test_no_google_baseline_acknowledges_a_system_parameter():
    """No `google_drive` or `gmail` baseline entry is a `missing_param` for `$.xgafv`,
    `access_token` or `oauth_token`: `openapi.google_system_parameters` declares all three."""
    from backlot.fidelity import Baseline, baseline_path

    for source in ("google_drive", "gmail"):
        acknowledged = Baseline.load(baseline_path(source)).acknowledged.values()
        for name in ("$.xgafv", "access_token", "oauth_token"):
            assert not [f.path for f in acknowledged if f.path.endswith(f"?{name}")], (
                source,
                name,
            )


# A request with no credential per row: the method, the path, what real answered it with no
# `Authorization` (403 an unregistered caller, 401 a missing credential, as
# `errors.google._UNREGISTERED_CALLER_OPERATIONS` records), and what the admin token gets on the
# same request, which on an operation Backlot does not serve is the 404 or 405 the path gets with
# no operation to run (`routers.google.refuse_an_unserved_operation`); None where that answer is
# not this test's. A code of 404 or 405 is the router's, whatever the credential.
# fmt: off
_NO_CREDENTIAL_ROWS = [
    ("GET", "/drive/v3/files", 403, 200),
    ("GET", "/drive/v3/files/{doc}", 403, 200),
    ("GET", "/drive/v3/files/{doc}/export?mimeType=text/plain&alt=json", 403, None),
    ("GET", "/drive/v3/files/{doc}/permissions", 401, 200),
    ("GET", "/drive/v3/about?fields=user", 401, 200),
    ("GET", "/drive/v3/drives", 401, 200),
    ("GET", "/sheets/v4/spreadsheets/x", 403, 404),
    ("GET", "/sheets/v4/spreadsheets/x/values/A1", 403, 404),
    ("GET", "/sheets/v4/spreadsheets/x/values:batchGet?ranges=A1", 403, 404),
    ("POST", "/sheets/v4/spreadsheets/x/values:batchGetByDataFilter", 401, 404),
    ("POST", "/sheets/v4/spreadsheets/x:getByDataFilter", 401, 404),
    ("GET", "/docs/v1/documents/x", 401, 404),
    ("GET", "/slides/v1/presentations/x", 401, 404),
    ("GET", "/gmail/v1/users/me/profile", 401, 200),
    ("GET", "/gmail/v1/users/me/labels", 401, 200),
    # the baselines' `missing` operations
    ("GET", "/drive/v3/changes/startPageToken", 401, 404),
    ("GET", "/drive/v3/files/generateIds", 401, 404),
    ("GET", "/drive/v3/files/trash", 403, 404),
    ("GET", "/drive/v3/files/{doc}/comments", 403, 404),
    ("POST", "/drive/v3/files/{doc}/comments", 403, 404),
    ("PATCH", "/drive/v3/files/{doc}/comments/c1", 403, 404),
    ("DELETE", "/drive/v3/files/{doc}/comments/c1", 403, 404),
    ("GET", "/drive/v3/files/{doc}/comments/c1/replies", 403, 404),
    ("GET", "/drive/v3/operations/o1", 403, 404),
    ("POST", "/drive/v3/files/{doc}/download", 403, 404),
    ("GET", "/drive/v3/apps", 403, 404),
    ("GET", "/drive/v3/apps/a1", 401, 404),
    ("PATCH", "/drive/v3/files/{doc}", 401, 405),
    ("DELETE", "/drive/v3/files/{doc}", 401, 405),
    ("POST", "/drive/v3/files/{doc}/copy", 401, 404),
    ("GET", "/sheets/v4/spreadsheets/x/developerMetadata/1", 401, 404),
    ("POST", "/sheets/v4/spreadsheets/x:batchUpdate", 401, 405),
    ("POST", "/docs/v1/documents/x:batchUpdate", 401, 405),
    ("GET", "/slides/v1/presentations/x/pages/p1", 401, 404),
    ("POST", "/gmail/v1/users/me/messages/send", 401, 405),
    ("GET", "/gmail/v1/users/me/drafts", 401, 404),
    ("DELETE", "/gmail/v1/users/me/messages/abc123", 401, 405),
    ("POST", "/gmail/v1/users/me/labels", 401, 405),
    # an escaped slash stays inside its segment (`routers.google._operation_path`)
    ("GET", "/drive/v3/files/abc%2Fpermissions", 403, 404),
    ("POST", "/drive/v3/files/{doc}%2Fcopy", 404, 404),
    # no operation Google publishes: the router's 405 whatever the credential, where real answered
    # its front end's HTML 404
    ("PUT", "/gmail/v1/users/me/profile", 405, 405),
    ("PUT", "/drive/v3/files/{doc}", 405, 405),
]
# fmt: on


@pytest.mark.parametrize("method, path, code, admin", _NO_CREDENTIAL_ROWS)
def test_a_missing_credential_is_answered_by_operation(client, admin_h, method, path, code, admin):
    """The rows `_NO_CREDENTIAL_ROWS` records: no `Authorization` at all is answered per operation
    rather than per family, a published operation Backlot does not serve included. Every 401 carries
    real's challenge and no 403 does, `Bearer nope` is the 401 on each published operation, and the
    admin token is what the request gets once its credential is read."""
    url = path.format(doc=_drive_find(client, admin_h, "Brand")["id"])
    body = _A1_FILTER if method in ("POST", "PATCH") else None
    r = client.request(method, url, json=body)
    if code in (404, 405):
        for headers in (BAD_TOKEN, admin_h):
            assert client.request(method, url, json=body, headers=headers).status_code == code
        assert r.status_code == code, r.text
        return
    assert r.status_code == code, r.text
    e = _gerr(r)
    if code == 403:
        assert (
            e["message"] == gerr.UNREGISTERED_CALLER_MESSAGE and e["status"] == "PERMISSION_DENIED"
        )
        assert "www-authenticate" not in r.headers
    else:
        assert e["message"] == gerr.MISSING_CREDENTIALS_MESSAGE and e["status"] == "UNAUTHENTICATED"
        assert r.headers["www-authenticate"] == _CHALLENGE
    bad = client.request(method, url, json=body, headers=BAD_TOKEN)
    assert bad.status_code == 401, bad.text
    assert bad.headers["www-authenticate"] == _CHALLENGED
    if admin is not None:
        assert client.request(method, url, json=body, headers=admin_h).status_code == admin


_SERVED = "served"  # the read's own 200
_NONE = "none"  # the operation's answer to no credential at all

# An `Authorization` value per row, `{t}` the admin token, and which of four answers real gave it
# on the two reads below, as ``routers.google._credential`` records: the read's 200, the bad token's
# 401 under either challenge (`_CHALLENGED`, `_CHALLENGE`), or what no credential gets.
# fmt: off
_CREDENTIAL_ROWS = [
    ("Bearer {t}", _SERVED), ("OAuth {t}", _SERVED), ("Bearer  {t}", _SERVED),
    ("Bearer \t{t}", _SERVED), ("Bearer {t} x", _SERVED), ("Bearer {t}\tx", _SERVED),
    ("Bearer {t},x", _SERVED), ("Bearer ,{t}", _SERVED),
    ('Bearer "{t}"', _SERVED), ('Bearer "{t}', _SERVED), ('OAuth "{t}"', _SERVED), ('Bearer ,"{t}"', _SERVED),
    ('Bearer "{t}" x', _SERVED), ('Bearer "{t}",x', _SERVED), ('Bearer "{t}"x', _SERVED),
    ('Bearer ",{t},"', _SERVED), ('Bearer "\t{t}"', _SERVED), ('Bearer "{t} "', _SERVED),
    ("Bearer nope", _CHALLENGED), ("OAuth nope", _CHALLENGED), ("Bearer x {t}", _CHALLENGED), ("Bearer {t};x", _CHALLENGED),
    ('Bearer {t}"', _CHALLENGED), ("Bearer nope x", _CHALLENGED),
    ('Bearer "nope"', _CHALLENGED), ('Bearer "{t} x"', _CHALLENGED), ('Bearer "{t},x"', _CHALLENGED),
    ('Bearer " "', _CHALLENGED), ("Bearer '{t}'", _CHALLENGED),
    ("Bearer{t}", _CHALLENGE), ("Bearer\t{t}", _CHALLENGE), ("Bearers {t}", _CHALLENGE),
    ("Bearer ,", _CHALLENGE), ("OAuth ,", _CHALLENGE), ("OAuth2 {t}", _CHALLENGE),
    ('Bearer ""', _CHALLENGE), ('Bearer "', _CHALLENGE), ('Bearer ""{t}', _CHALLENGE),
    ("bearer {t}", _NONE), ("BEARER {t}", _NONE), ("oauth {t}", _NONE), ("OAUTH {t}", _NONE),
    ("token {t}", _NONE), ("Token {t}", _NONE), ("MAC {t}", _NONE), ("Basic Zm9vOmJhcg==", _NONE),
    ("{t}", _NONE), ("Bearer", _NONE), ("OAuth", _NONE),
]
# fmt: on


# A query per row, beside an `Authorization` value or none, and which of the same four answers real
# gave a query of that shape, as ``routers.google._query_credential`` records. `ya29.x` stands for a
# token of Google's shape that does not resolve, `{t}` for the admin token and `{m}` for the scoped
# one, neither of that shape.
# fmt: off
_QUERY_CREDENTIAL_ROWS = [
    (None, "access_token=ya29.x", _CHALLENGED), (None, "access_token=1/x", _CHALLENGED),
    (None, "access_token=1%2Fx", _CHALLENGED), (None, "bearer_token=ya29.x", _CHALLENGED),
    (None, "access%5Ftoken=ya29.x", _CHALLENGED),
    (None, "oauth_token=ya29.x", _CHALLENGED),
    (None, "access_token={t}", _NONE), (None, "access_token=ya29", _NONE),
    (None, "access_token=YA29.x", _NONE), (None, "access_token=%20ya29.x", _NONE),
    (None, "access_token=", _NONE), (None, "ACCESS_TOKEN=ya29.x", _NONE),
    # the first of `QUERY_TOKENS` the query carries is the gate, the last the token
    (None, "access_token=ya29.x&oauth_token={t}", _SERVED),
    (None, "oauth_token={t}&access_token=ya29.x", _SERVED),
    (None, "access_token=ya29.x&bearer_token={t}", _SERVED),
    (None, "bearer_token=ya29.x&oauth_token={t}", _SERVED),
    (None, "access_token={t}&oauth_token=ya29.x", _NONE),
    (None, "access_token=&oauth_token=ya29.x", _NONE),
    (None, "oauth_token=nope&access_token=ya29.x", _CHALLENGED),
    (None, "oauth_token=&access_token=ya29.x", _CHALLENGE),
    (None, "access_token=ya29.x&access_token={t}", _CHALLENGE),
    (None, "access_token=ya29.x&oauth_token={t}&oauth_token={t}", _CHALLENGE),
    (None, "access_token=ya29.x&access_token=ya29.x&oauth_token={t}", _SERVED),
    (None, "access_token=ya29.x&oauth_token={m}", _SERVED),
    # beside a header: a credential, a bare scheme and `Bearer ,` outrank the query, a scheme run
    # into its token reads it without the gate, and a header that is no credential leaves it alone
    ("Bearer {t}", "access_token=ya29.x", _SERVED),
    ("Bearer nope", "access_token=ya29.x&oauth_token={t}", _CHALLENGED),
    ("Bearer ,", "access_token=ya29.x&oauth_token={t}", _CHALLENGE),
    ('Bearer ""', "access_token=ya29.x&oauth_token={t}", _CHALLENGE),
    ('Bearer "nope"', "access_token=ya29.x&oauth_token={t}", _CHALLENGED),
    ('Bearer "{t}"', "access_token=ya29.x", _SERVED),
    ("Bearer", "access_token=ya29.x&oauth_token={t}", _NONE),
    ("Bearer{t}", "access_token={t}", _SERVED), ("OAuth2 {t}", "access_token={t}", _SERVED),
    ("Bearer{m}", "access_token={m}", _SERVED),
    ("Bearer{t}", "access_token=nope", _CHALLENGED), ("Bearer{t}", "access_token=", _CHALLENGE),
    ("Bearer{t}", "ACCESS_TOKEN={t}", _CHALLENGE),
    ("Bearer{t}", "access_token=&oauth_token={t}", _SERVED),
    ("Bearer{t}", "oauth_token=&access_token={t}", _CHALLENGE),
    ("bearer {t}", "access_token=ya29.x&oauth_token={t}", _SERVED),
    ("Basic Zm9vOmJhcg==", "access_token=ya29.x", _CHALLENGED), ("", "access_token=ya29.x", _CHALLENGED),
]
# fmt: on


@pytest.mark.parametrize(
    "value, query, answer",
    [(value, "", answer) for value, answer in _CREDENTIAL_ROWS] + _QUERY_CREDENTIAL_ROWS,
)
@pytest.mark.parametrize(
    "path, anonymous", [("/gmail/v1/users/me/labels/INBOX", 401), ("/drive/v3/files", 403)]
)
def test_a_credential_is_read_as_google_reads_it(
    client, tokens_yaml, tokens, value, query, answer, path, anonymous
):
    """The rows `_CREDENTIAL_ROWS` and `_QUERY_CREDENTIAL_ROWS` record, on an operation whose caller
    with no credential is a missing one and on one whose is an unregistered one. A served row is
    answered as its token is under `Authorization: Bearer`, the scoped token's as the scoped user's
    rather than the admin's."""
    admin, scoped = tokens_yaml["admin_token"], tokens["mia@acme.com"]
    headers = {} if value is None else {"Authorization": value.format(t=admin, m=scoped)}
    sent = query.format(t=admin, m=scoped)
    r = client.get(f"{path}?{sent}" if sent else path, headers=headers)
    if answer == _SERVED:
        assert r.status_code == 200, r.text
        token = scoped if "{m}" in f"{value}{query}" else admin
        assert r.json() == client.get(path, headers={"Authorization": f"Bearer {token}"}).json()
        if token == scoped:
            assert r.json() != client.get(path, headers={"Authorization": f"Bearer {admin}"}).json()
        return
    if answer == _NONE:
        assert r.status_code == anonymous, r.text
        assert r.json() == client.get(path).json()
        return
    assert r.status_code == 401, r.text
    assert _gerr(r)["message"] == gerr.BAD_TOKEN_MESSAGE
    assert r.headers["www-authenticate"] == answer


_DENIED = "Delegation denied for mia@acme.com"
_NOT_AN_ADDRESS = "Invalid user id specified in request/Delegation denied"

# A Gmail request per row as a user's token sends it, `{u}` the mailbox the path names: the user,
# the `userId`, the rest of the path and its query, and the status and message real answered (the
# admin row aside), as ``routers.google._refuse_another_mailbox`` records, None where the message
# is not a refusal's.
# fmt: off
_MAILBOX_ROWS = [
    ("mia", "me", "profile", 200, None),
    ("mia", "ME", "profile", 200, None),
    ("mia", "Me", "profile", 200, None),
    ("mia", "MIA@ACME.COM", "profile", 200, None),
    ("mia", "mia@acme.com", "messages?maxResults=1", 200, None),
    ("mia", "ava.chen@acme.com", "profile", 403, _DENIED),
    ("mia", "someone@example.com", "profile", 403, _DENIED),
    ("mia", "someone@example.com", "labels", 403, _DENIED),
    ("mia", "someone@example.com", "labels/INBOX", 403, _DENIED),
    ("mia", "someone@example.com", "messages", 403, _DENIED),
    ("mia", "someone@example.com", "messages/{comp}", 403, _DENIED),
    ("mia", "someone@example.com", "messages/{comp}/attachments/x", 403, _DENIED),
    ("mia", "someone@example.com", "threads", 403, _DENIED),
    ("mia", "someone@example.com", "threads/{comp}", 403, _DENIED),
    ("mia", "bob", "profile", 403, _NOT_AN_ADDRESS),
    ("mia", "@", "profile", 403, _NOT_AN_ADDRESS),
    ("mia", "mia@", "profile", 403, _NOT_AN_ADDRESS),
    ("mia", "@acme.com", "profile", 403, _NOT_AN_ADDRESS),
    # what comes before it, and what after
    ("bad", "someone@example.com", "profile", 401, None),
    ("mia", "someone@example.com", "profile?%24.xgafv=9", 400, None),
    ("mia", "someone@example.com", "messages?maxResults=abc", 400, None),
    ("mia", "someone@example.com", "messages?maxResults=-1", 400, None),
    ("mia", "someone@example.com", "threads?maxResults=abc", 400, None),
    ("mia", "bob", "messages?maxResults=abc", 400, None),
    ("mia", "someone@example.com", "messages/zzz", 403, _DENIED),
    ("mia", "someone@example.com", "messages?maxResults=0", 403, _DENIED),
    ("mia", "someone@example.com", "messages?pageToken=zzz", 403, _DENIED),
    # the admin token reads any mailbox (`_refuse_another_mailbox` says what it stands for)
    ("admin", "someone@example.com", "profile", 200, None),
]
# fmt: on


@pytest.mark.parametrize("caller, user_id, rest, status, message", _MAILBOX_ROWS)
def test_gmail_a_users_token_reads_its_own_mailbox_only(
    client, admin_h, tokens, ro_conn, caller, user_id, rest, status, message
):
    """The rows `_MAILBOX_ROWS` records. The refusal is real's to the byte, and a user's own mailbox
    named any way it can be is answered as `me` is, the address it reports included."""
    comp = ro_conn.execute(
        "SELECT id FROM gmail_messages WHERE title LIKE 'Confidential comp%'"
    ).fetchone()["id"]
    headers = {
        "mia": {"Authorization": f"Bearer {tokens['mia@acme.com']}"},
        "bad": BAD_TOKEN,
        "admin": admin_h,
    }[caller]
    r = client.get(f"/gmail/v1/users/{user_id}/{rest.format(comp=comp)}", headers=headers)
    assert r.status_code == status, r.text
    if message is not None:
        assert r.json() == {
            "error": {
                "code": 403,
                "message": message,
                "errors": [{"message": message, "domain": "global", "reason": "forbidden"}],
                "status": "PERMISSION_DENIED",
            }
        }
    if status == 200 and rest == "profile":
        me = client.get("/gmail/v1/users/me/profile", headers=headers).json()
        assert r.json() == me if caller == "mia" else r.json()["emailAddress"] == user_id


# An unserved operation per row (`routers.google._PUBLISHED_NOT_SERVED`), beside a system parameter:
# the method, the path, the query, the credential, and what real answered, as
# ``refuse_an_unserved_operation`` records — the status and the error code inside it, which a
# `callback` on a GET wraps at 200. A POST and a PATCH leave `callback` alone, as a served POST
# does. An anonymous GET's `callback=cb` is a row of `JSONP_FAMILY_ERRORS` instead.
# fmt: off
_UNSERVED_PARAMETER_ROWS = [
    ("GET", "/drive/v3/changes/startPageToken", {"$.xgafv": "9"}, None, 400, 400),
    ("GET", "/drive/v3/changes/startPageToken", {"callback": "cb"}, "bad", 200, 401),
    ("GET", "/drive/v3/changes/startPageToken", {"callback": "a b"}, None, 200, 400),
    ("PATCH", "/drive/v3/files/x", {"$.xgafv": "9"}, "bad", 400, 400),
    ("PATCH", "/drive/v3/files/x", {"callback": "cb"}, None, 401, 401),
    ("PATCH", "/drive/v3/files/x", {"callback": "a b"}, None, 401, 401),
    ("POST", "/gmail/v1/users/me/messages/send", {"callback": "a b"}, "bad", 401, 401),
    ("GET", "/drive/v3/files/x/revisions/y", {"alt": "media", "access_token": ""}, None, 401, 401),
]
# fmt: on


@pytest.mark.parametrize("method, path, params, caller, status, code", _UNSERVED_PARAMETER_ROWS)
def test_an_unserved_operation_reads_the_system_parameters_first(
    client, method, path, params, caller, status, code
):
    """The rows `_UNSERVED_PARAMETER_ROWS` records: ahead of the credential, `$.xgafv` refused,
    and on a GET a `callback` wrapping the refusal or refused itself, as on a served route; and a
    revision's `alt=media` beside an empty `access_token` left to the missing credential's 401
    rather than a download's answer. The check runs ahead of routing here
    (``backlot.main.route_a_google_path_the_way_google_does``), where a served route's runs as the
    router's dependency, so it is pinned on its own."""
    headers = BAD_TOKEN if caller == "bad" else {}
    r = client.request(method, path, params=params, headers=headers)
    assert r.status_code == status, r.text
    error = _jsonp(r, params["callback"]) if status == 200 else _gerr(r)
    assert error["error"]["code"] == code if status == 200 else error["code"] == code
    if status == 200 and params["callback"] == "cb":
        plain = client.request(method, path, headers=headers)
        assert r.text == f"// API callback\ncb({plain.text});"
    if code == 401:
        assert r.headers["www-authenticate"] == (_CHALLENGED if caller == "bad" else _CHALLENGE)


# --- a `HEAD`, and a trailing slash ------------------------------------------------------------

_REFUSED = "refused"  # the editor hosts' empty 400 (`errors.google.head_refusal`)

# A `HEAD` per row, answered as the `GET` of the same URL (or of the URL in the third column) with
# the body left off, or refused. The credential is `admin`, `mia` (the scoped token, which can see
# neither the spreadsheet nor the CFO's message), `bad` or None for no header; the status is the
# `HEAD`'s, and the GET's too where the row is not refused; the last column is whether the `HEAD`
# declares a length (`errors.google.head_content_length`).
# fmt: off
_HEAD_ROWS = [
    ("/drive/v3/files", "admin", None, 200, False),
    ("/drive/v3/files", None, None, 403, False),
    ("/drive/v3/files", "bad", None, 401, False),
    ("/drive/v3/files/{doc}", "admin", None, 200, False),
    ("/drive/v3/files/{sheet}", "mia", None, 404, False),
    ("/drive/v3/files/nosuchfile000", "admin", None, 404, False),
    ("/drive/v3/files/{doc}/export?mimeType=text/plain", "admin", None, 200, True),
    ("/drive/v3/files/{sheet}/export?mimeType=text/csv", "mia", None, 404, False),
    ("/drive/v3/files/{doc}/export?mimeType=text/plain", None, None, 403, False),
    ("/drive/v3/files/{doc}/export?mimeType=text/plain&callback=cb", "admin", None, 200, True),
    ("/drive/v3/files/{pdf}?alt=media", "admin", None, 200, True),
    ("/drive/v3/files/{doc}?alt=media", "admin", None, 403, False),
    ("/drive/v3/files/{pdf}?alt=media&callback=a%20b", "admin", None, 503, False),
    ("/drive/v3/files/{doc}/permissions", "admin", None, 200, False),
    ("/drive/v3/about?fields=user", "admin", None, 200, False),
    ("/drive/v3/about", "admin", None, 400, False),
    ("/drive/v3/drives", "admin", None, 200, False),
    ("/drive/v3/files?pageSize=0", "admin", None, 400, False),
    ("/drive/v3/files?%24.xgafv=9", "admin", None, 400, False),
    # each answered as the read without its `callback` (``errors.google.jsonp_callback``)
    ("/drive/v3/files?callback=cb", "admin", "/drive/v3/files", 200, False),
    ("/drive/v3/files?callback=a%20b", "admin", "/drive/v3/files", 200, False),
    ("/drive/v3/files?callback=cb", "bad", "/drive/v3/files", 401, False),
    ("/gmail/v1/users/me/profile?callback=a%20b", None, "/gmail/v1/users/me/profile", 401, False),
    ("/drive/v3/files/", "admin", None, 200, False),
    ("/gmail/v1/users/me/profile", "admin", None, 200, False),
    ("/gmail/v1/users/me/profile", None, None, 401, False),
    ("/gmail/v1/users/me/profile", "bad", None, 401, False),
    ("/gmail/v1/users/me/profile?callback=cb", "admin", "/gmail/v1/users/me/profile", 200, False),
    ("/gmail/v1/users/me/labels", "admin", None, 200, False),
    ("/gmail/v1/users/me/labels/INBOX", "admin", None, 200, False),
    ("/gmail/v1/users/me/labels/", "admin", None, 200, False),
    ("/gmail/v1/users/me/messages", "admin", None, 200, False),
    ("/gmail/v1/users/me/messages?maxResults=0", "admin", None, 400, False),
    ("/gmail/v1/users/me/messages/{comp}", "admin", None, 200, False),
    ("/gmail/v1/users/me/messages/{comp}", "mia", None, 404, False),
    ("/gmail/v1/users/me/messages/zzz", "admin", None, 400, False),
    ("/gmail/v1/users/me/threads", "admin", None, 200, False),
    ("/gmail/v1/users/me/threads/{comp}", "admin", None, 200, False),
    ("/drive/v3/changes/startPageToken", None, None, 401, False),
    ("/sheets/v4/spreadsheets/{sheet}", "admin", _REFUSED, 400, False),
    ("/sheets/v4/spreadsheets/{sheet}", None, _REFUSED, 400, False),
    ("/sheets/v4/spreadsheets/{sheet}/values/A1", "admin", _REFUSED, 400, False),
    ("/sheets/v4/spreadsheets/{sheet}?%24.xgafv=9", "admin", _REFUSED, 400, False),
    ("/sheets/v4/spreadsheets/{sheet}/", "admin", _REFUSED, 400, False),
    ("/sheets/v4/nope", "admin", _REFUSED, 400, False),
    ("/sheets/v4x/spreadsheets/abc", None, _REFUSED, 400, False),
    ("/docs/v1/documents/{doc}", "bad", _REFUSED, 400, False),
    ("/slides/v1/presentations/x", None, _REFUSED, 400, False),
]
# fmt: on


@pytest.mark.parametrize("path, caller, get_path, status, declares", _HEAD_ROWS)
def test_google_a_head_is_the_get_without_its_body_or_the_editor_hosts_400(
    client, admin_h, tokens, ro_conn, path, caller, get_path, status, declares
):
    """The rows `_HEAD_ROWS` records. Drive and Gmail answer the `HEAD` as the GET, status and
    `content-type` alike, with a length only where `declares` says; the three editor hosts answer
    their empty 400, where the GET beside it is a JSON answer of its own. The body a `HEAD` leaves
    off is read from the ASGI messages the app sends, since the test client discards it."""
    from starlette.testclient import TestClient

    ids = {
        "doc": _drive_find(client, admin_h, "Brand")["id"],
        "pdf": _drive_find(client, admin_h, "Whitepaper")["id"],
        "sheet": _drive_find(client, admin_h, "Q1 Revenue Model")["id"],
        "comp": ro_conn.execute(
            "SELECT id FROM gmail_messages WHERE title LIKE 'Confidential comp%'"
        ).fetchone()["id"],
    }
    headers = {
        "admin": admin_h,
        "mia": {"Authorization": f"Bearer {tokens['mia@acme.com']}"},
        "bad": BAD_TOKEN,
        None: {},
    }[caller]
    url = path.format(**ids)
    head = client.head(url, headers=headers)
    get = client.get(url if get_path in (None, _REFUSED) else get_path, headers=headers)
    assert head.status_code == status, url
    if get_path == _REFUSED:
        assert head.headers["content-type"] == "text/html; charset=utf-8", url
        assert head.headers["content-length"] == "0", url
        assert get.headers["content-type"].startswith("application/json"), url
    else:
        assert get.status_code == status, url
        assert head.headers["content-type"] == get.headers["content-type"], url
        if declares:
            assert head.headers["content-length"] == str(len(get.content)), url
        else:
            assert "content-length" not in head.headers, url
    sent = []

    async def recording(scope, receive, send):
        async def record(message):
            sent.append(message)
            await send(message)

        await client.app(scope, receive, record)

    TestClient(recording).head(url, headers=headers)  # outside `with`, so `client` keeps its state
    assert b"".join(m.get("body", b"") for m in sent if m["type"] == "http.response.body") == b""


# A slashed request per row, the spellings measured for
# ``backlot.main.route_a_google_path_the_way_google_does``: the slashed request, the request it
# answers as (None where that is the unrouted 404), the credential as in `_HEAD_ROWS`, and the
# status.
# fmt: off
_SLASHED_ROWS = [
    ("GET /drive/v3/files/", "GET /drive/v3/files", "admin", 200),
    ("GET /drive/v3/files//", "GET /drive/v3/files", "admin", 200),
    ("GET /drive/v3/files/", "GET /drive/v3/files", None, 403),
    ("GET /drive/v3/files/{doc}/", "GET /drive/v3/files/{doc}", "admin", 200),
    ("GET /drive/v3/files/{sheet}/", "GET /drive/v3/files/{sheet}", "mia", 404),
    ("GET /drive/v3/files/nosuchfile000/", "GET /drive/v3/files/nosuchfile000", "admin", 404),
    ("GET /drive/v3/files/{doc}/export/?mimeType=text/plain", "GET /drive/v3/files/{doc}/export?mimeType=text/plain", "admin", 200),
    ("GET /drive/v3/files/{doc}/permissions/", "GET /drive/v3/files/{doc}/permissions", "admin", 200),
    ("GET /drive/v3/about/?fields=user", "GET /drive/v3/about?fields=user", "admin", 200),
    ("GET /drive/v3/about/?fields=user", "GET /drive/v3/about?fields=user", None, 401),
    ("GET /drive/v3/drives/", "GET /drive/v3/drives", "admin", 200),
    ("GET /gmail/v1/users/me/profile/", "GET /gmail/v1/users/me/profile", None, 401),
    ("GET /gmail/v1/users/me/profile//", "GET /gmail/v1/users/me/profile", "admin", 200),
    ("GET /gmail/v1/users/me/labels//", "GET /gmail/v1/users/me/labels", "admin", 200),
    ("GET /gmail/v1/users/me/labels///", "GET /gmail/v1/users/me/labels", "admin", 200),
    ("GET /gmail/v1/users/me/labels/", "GET /gmail/v1/users/me/labels", "bad", 401),
    ("GET /gmail/v1/users/me/labels/INBOX/", "GET /gmail/v1/users/me/labels/INBOX", "admin", 200),
    ("GET /gmail/v1/users/me/messages/?maxResults=1", "GET /gmail/v1/users/me/messages?maxResults=1", "admin", 200),
    ("GET /gmail/v1/users/me/messages/{comp}/", "GET /gmail/v1/users/me/messages/{comp}", "mia", 404),
    ("GET /gmail/v1/users/me/messages/{held}/attachments/{att}/", "GET /gmail/v1/users/me/messages/{held}/attachments/{att}", "admin", 200),
    ("GET /gmail/v1/users/me/threads/", "GET /gmail/v1/users/me/threads", "admin", 200),
    ("GET /gmail/v1/users/me/threads/{comp}/", "GET /gmail/v1/users/me/threads/{comp}", "admin", 200),
    ("GET /sheets/v4/spreadsheets/{sheet}/", "GET /sheets/v4/spreadsheets/{sheet}", "admin", 200),
    ("GET /sheets/v4/spreadsheets/{sheet}//", "GET /sheets/v4/spreadsheets/{sheet}", None, 403),
    ("GET /sheets/v4/spreadsheets/{sheet}/values/A1/", "GET /sheets/v4/spreadsheets/{sheet}/values/A1", "admin", 200),
    ("GET /sheets/v4/spreadsheets/{sheet}/values/A1//", "GET /sheets/v4/spreadsheets/{sheet}/values/A1", "admin", 200),
    # an escaped slash is part of the range, and an empty range is no route at all
    ("GET /sheets/v4/spreadsheets/{sheet}/values/A1%2F", "GET /sheets/v4/spreadsheets/{sheet}/values/A1%2F", "admin", 400),
    ("GET /sheets/v4/spreadsheets/{sheet}/values/", None, "admin", 404),
    ("GET /sheets/v4/spreadsheets/{sheet}/values", None, "admin", 404),
    ("GET /sheets/v4/spreadsheets/{sheet}/values:batchGet/?ranges=A1", "GET /sheets/v4/spreadsheets/{sheet}/values:batchGet?ranges=A1", "admin", 200),
    ("POST /sheets/v4/spreadsheets/{sheet}:getByDataFilter/", "POST /sheets/v4/spreadsheets/{sheet}:getByDataFilter", "admin", 200),
    ("GET /docs/v1/documents/{doc}/", "GET /docs/v1/documents/{doc}", None, 401),
    ("GET /slides/v1/presentations/x/", "GET /slides/v1/presentations/x", None, 401),
    ("GET /drive/v3//files", None, "admin", 404),
    ("GET /gmail/v1/users/me//labels", None, "admin", 404),
]
# fmt: on


@pytest.mark.parametrize("slashed, slash_free, caller, status", _SLASHED_ROWS)
def test_google_a_trailing_slash_is_not_part_of_the_path(
    client, admin_h, tokens, ro_conn, slashed, slash_free, caller, status
):
    """The rows `_SLASHED_ROWS` records, each answered byte for byte as the request beside it where
    it has one (``backlot.main.route_a_google_path_the_way_google_does``)."""
    held = ro_conn.execute(
        "SELECT id FROM gmail_messages WHERE COALESCE(attachments,'') NOT IN ('', '[]') LIMIT 1"
    ).fetchone()["id"]
    parts = client.get(f"/gmail/v1/users/me/messages/{held}", headers=admin_h).json()["payload"]
    ids = {
        "doc": _drive_find(client, admin_h, "Brand")["id"],
        "sheet": _drive_find(client, admin_h, "Q1 Revenue Model")["id"],
        "comp": ro_conn.execute(
            "SELECT id FROM gmail_messages WHERE title LIKE 'Confidential comp%'"
        ).fetchone()["id"],
        "held": held,
        "att": next(p for p in parts["parts"] if p.get("filename"))["body"]["attachmentId"],
    }
    headers = {
        "admin": admin_h,
        "mia": {"Authorization": f"Bearer {tokens['mia@acme.com']}"},
        "bad": BAD_TOKEN,
        None: {},
    }[caller]

    def send(request):
        method, url = request.split(" ", 1)
        body = "{}" if method == "POST" else None
        return client.request(
            method, url.format(**ids), headers=headers, content=body, follow_redirects=False
        )

    got = send(slashed)
    assert got.status_code == status, got.text
    if slash_free is not None:
        assert got.content == send(slash_free).content


# --- `callback`, and the bytes a Google body reaches the wire as -------------------------------
#
# Measured 2026-09-15 against sheets.googleapis.com, docs.googleapis.com, drive/v3 on
# www.googleapis.com, gmail.googleapis.com and slides.googleapis.com — every family, authenticated
# where a credential reaches one and anonymous where it does not, over a 400, a 401, a 403 and a
# 404. Three properties, none of which `backlot diff` can see: it builds its findings from the
# discovery document's operations and parameters, so no comparison it runs reads a response body.
#
#   callback=cb   HTTP 200, `text/javascript; charset=UTF-8`, `// API callback\ncb({…}\n);`
#   indentation   two spaces and a trailing newline on an error, whatever `prettyPrint` says
#   charset       `application/json; charset=UTF-8` on the plain body
#
# The anonymous errors below are one GET per family from `_NO_CREDENTIAL_ROWS`, because one request
# per family is what real was asked, then three operations Backlot does not serve
# (``routers.google.refuse_an_unserved_operation``). Real answered those three as it does the five
# on 2026-10-13: indented under the charset, the script around the plain body, an empty `callback`
# as none, and `alt` spelt `json`, `JSON`, `Json` or empty keeping the script.

JSONP_FAMILY_ERRORS = [
    ("/drive/v3/files", 403),
    ("/sheets/v4/spreadsheets/x", 403),
    ("/gmail/v1/users/me/profile", 401),
    ("/docs/v1/documents/x", 401),
    ("/slides/v1/presentations/x", 401),
    ("/drive/v3/changes/startPageToken", 401),
    ("/drive/v3/files/x/comments", 403),
    ("/gmail/v1/users/me/drafts", 401),
]

CALLBACK_REFUSAL = (
    "Invalid JSONP callback name: '{}'; only alphabet, number, '_', '$', '.', '[' and ']' "
    "are allowed."
)


def _jsonp(resp, name):
    """The object inside a JSONP answer, having asserted that it IS one, called through ``name``.

    ``resp.json()`` cannot read these: the body is a script, which is the whole divergence. A name
    carrying a character the serializer escapes reaches the wrapper in that form, so it is compared
    through the same function — what that function must produce is pinned literally, and against
    the live sweep it came from, in ``test_the_characters_the_serializer_escapes``."""
    assert resp.status_code == 200, resp.text
    assert resp.headers["content-type"] == "text/javascript; charset=UTF-8"
    called = gerr._escaped_name(name)
    prefix = f"// API callback\n{called}("
    assert resp.text.startswith(prefix), resp.text[: len(prefix) + 20]
    assert resp.text.endswith(");")
    return json.loads(resp.text[len(prefix) : -2])


@pytest.mark.parametrize("path, code", JSONP_FAMILY_ERRORS)
def test_a_google_error_is_indented_and_names_its_charset(client, path, code):
    """Two spaces deep, one trailing newline, `application/json; charset=UTF-8`. A client that
    records a response body byte for byte, or one that sniffs the charset off the header, saw a
    different body from Backlot on every Google error before this."""
    r = client.get(path)
    assert r.status_code == code
    assert r.headers["content-type"] == "application/json; charset=UTF-8"
    assert r.text == json.dumps(r.json(), ensure_ascii=False, indent=2) + "\n"
    assert r.text.startswith('{\n  "error": {\n    "code": ')
    assert r.text.endswith("\n  }\n}\n")


def test_the_indented_error_is_the_body_real_sends_to_the_byte(client):
    """One family spelled out, so the shape is pinned by something other than the serializer that
    produced it. Sheets answers an anonymous GET 403 PERMISSION_DENIED with the
    unregistered-caller sentence."""
    r = client.get("/sheets/v4/spreadsheets/x")
    assert r.text == (
        "{\n"
        '  "error": {\n'
        '    "code": 403,\n'
        f'    "message": "{gerr.UNREGISTERED_CALLER_MESSAGE}",\n'
        '    "status": "PERMISSION_DENIED"\n'
        "  }\n"
        "}\n"
    )


@pytest.mark.parametrize("pretty", [None, "false", "true", "NOPE"])
def test_prettyprint_does_not_reach_an_error(base, admin_h, sheet_id, pretty):
    """Measured on all five families, each on an error of its own: every one came back two-space
    indented with no parameter, with `false` and with `true` alike, byte for byte. A SUCCESS under
    `false` is compact, which is what makes an indented error a rule and not a default — that half
    is `test_pretty_print_indents_by_default_and_is_compact_to_the_byte_when_off`, pinned to the
    byte both ways."""
    params = {} if pretty is None else {"prettyPrint": pretty}
    bad = _values(base, admin_h, sheet_id, "NOPE!!", **params)
    assert bad.status_code == 400, bad.text
    assert bad.text == json.dumps(bad.json(), ensure_ascii=False, indent=2) + "\n"


@pytest.mark.parametrize("path, code", JSONP_FAMILY_ERRORS)
def test_a_callback_answers_an_error_at_200_as_a_script(client, path, code):
    """JSONP is the case where the status code is the whole point: a browser loading the answer
    through a `<script>` element can see no status at all, so against real a failed call runs
    `cb({"error": …})` and the page handles it. Answering the error status with an unwrapped body
    fires `onerror` instead and the callback never runs — the client's error branch is then the one
    path its tests cannot exercise against Backlot."""
    plain = client.get(path)
    r = client.get(path, params={"callback": "cb"})
    assert r.status_code == 200
    assert r.headers["content-type"] == "text/javascript; charset=UTF-8"
    assert r.text == f"// API callback\ncb({plain.text});"
    assert json.loads(r.text[len("// API callback\ncb(") : -2])["error"]["code"] == code
    # the 401's challenge rides on the script too (``errors.google._CHALLENGE``)
    assert r.headers.get("www-authenticate") == (_CHALLENGE if code == 401 else None)


def test_a_callback_wraps_a_success_and_an_error_through_the_same_serializer(
    base, admin_h, sheet_id
):
    """The divergence this closes was between Backlot's own two paths as much as against the
    vendor: `_sheets_respond` already rendered a success to the byte while every error went out as
    one compact `JSONResponse` line.

    The success is compared to its own unwrapped body rather than read once, which is the assertion
    `test_callback_wraps_the_body_as_jsonp` does not make — that one reads the prefix and the
    suffix. Wrapping a success byte for byte is held here and nowhere else."""
    ok = _values(base, admin_h, sheet_id, "Sheet1!A1")
    ok_cb = _values(base, admin_h, sheet_id, "Sheet1!A1", callback="cb")
    bad = _values(base, admin_h, sheet_id, "NOPE!!")
    bad_cb = _values(base, admin_h, sheet_id, "NOPE!!", callback="cb")
    assert ok_cb.text == f"// API callback\ncb({ok.text});"
    assert bad_cb.text == f"// API callback\ncb({bad.text});"
    assert ok_cb.headers["content-type"] == bad_cb.headers["content-type"]
    assert (ok_cb.status_code, bad_cb.status_code) == (200, 200)


@pytest.mark.parametrize("path, code", JSONP_FAMILY_ERRORS)
def test_an_empty_callback_is_no_callback(client, path, code):
    """Measured: `callback=` answers the plain body at the real status, success and error alike."""
    r = client.get(path, params={"callback": ""})
    assert r.status_code == code
    assert r.headers["content-type"] == "application/json; charset=UTF-8"
    assert r.text == client.get(path).text


@pytest.mark.parametrize("name", ["a b", "cb);alert(1", "<script>", "window['x']", "cb\n", "é"])
def test_a_callback_name_that_cannot_be_one_is_refused_through_itself(client, admin_h, name):
    """The refusal arrives WRAPPED, through the very name it refuses — measured on Sheets, Drive
    and Gmail, authenticated and anonymous. So a page that asked for a callback still gets a script
    that calls something, and the error object reaches its error branch rather than `onerror`."""
    r = client.get("/sheets/v4/spreadsheets/x", headers=admin_h, params={"callback": name})
    assert _jsonp(r, name)["error"] == {
        "code": 400,
        "message": CALLBACK_REFUSAL.format(name),
        "status": "INVALID_ARGUMENT",
    }


@pytest.mark.parametrize("name", ["cb", "1bad", "foo.bar", "$cb", "_cb", ".cb", "cb.", "a" * 2000])
def test_the_callback_names_real_accepts(client, admin_h, name):
    """Position does not matter and length does not either — measured, `.cb`, `cb.`, `1bad` and a
    2,000-character name are all accepted, so the rule is the character set and nothing else."""
    r = client.get("/sheets/v4/spreadsheets/x", headers=admin_h, params={"callback": name})
    assert r.text.startswith(f"// API callback\n{name}(")
    assert "Invalid JSONP callback name" not in r.text


@pytest.mark.parametrize("ch", list("!\"#%&'()*+,-/:;<=>?@\\^`{|}~ ") + ["\t", "\n", "é", "​"])
def test_the_characters_a_callback_name_may_not_hold(client, admin_h, ch):
    """The live sweep this mirrors sent `cb<CH>x` for each ASCII punctuation mark and for space, tab
    and newline, and `a<ZWSP>b` besides. `é` and a zero-width space are refused among them, so
    "alphabet" in real's own sentence is ASCII letters and nothing wider."""
    r = client.get("/sheets/v4/spreadsheets/x", headers=admin_h, params={"callback": f"cb{ch}x"})
    assert _jsonp(r, f"cb{ch}x")["error"]["message"] == CALLBACK_REFUSAL.format(f"cb{ch}x")


@pytest.mark.parametrize("ch", list("$.[]_09Az"))
def test_the_characters_a_callback_name_may_hold(client, admin_h, ch):
    """The other half of the same sweep: these seven classes are what the refusal names."""
    r = client.get("/sheets/v4/spreadsheets/x", headers=admin_h, params={"callback": f"cb{ch}x"})
    assert "Invalid JSONP callback name" not in r.text


def test_the_callback_refusal_beats_the_route_but_not_the_system_parameter(client, admin_h):
    """Measured, both directions. `callback=a b` answers its own 400 ahead of a bad token, a
    missing credential, an unparseable range and a mistyped `fields` mask; `$.xgafv=9` beside it
    answers the `$.xgafv` sentence instead — wrapped through the very name the other check would
    have refused. That order is why both live in ``validate_system_parameters``, `$.xgafv` first."""
    for headers, path, params in (
        (BAD_TOKEN, "/sheets/v4/spreadsheets/x", {}),
        ({}, "/sheets/v4/spreadsheets/x", {}),
        (admin_h, "/sheets/v4/spreadsheets/x/values/NOPE!!", {}),
        (admin_h, "/drive/v3/files", {"fields": "nope"}),
    ):
        r = client.get(path, headers=headers, params={"callback": "a b", **params})
        assert _jsonp(r, "a b")["error"]["message"] == CALLBACK_REFUSAL.format("a b"), path
    r = client.get(
        "/sheets/v4/spreadsheets/x", headers=admin_h, params={"callback": "a b", "$.xgafv": "9"}
    )
    assert _jsonp(r, "a b")["error"]["message"] == XGAFV_REFUSAL.format("9")


def test_the_callback_refusal_carries_its_familys_errors_array(client, admin_h):
    """`badRequest` under `global`, which is :func:`gerr.invalid_argument` — measured on Sheets at
    `$.xgafv=1` and on Drive, which carries the array with no parameter at all."""
    entry = {
        "message": CALLBACK_REFUSAL.format("a b"),
        "domain": "global",
        "reason": "badRequest",
    }
    sheets = client.get(
        "/sheets/v4/spreadsheets/x", headers=admin_h, params={"callback": "a b", "$.xgafv": "1"}
    )
    assert _jsonp(sheets, "a b")["error"]["errors"] == [entry]
    drive = client.get("/drive/v3/files", headers=admin_h, params={"callback": "a b"})
    assert _jsonp(drive, "a b")["error"]["errors"] == [entry]


@pytest.mark.parametrize(
    "alt, message", [("media", "Unsupported alt type"), ("zzz", "Invalid value")]
)
def test_an_alt_the_api_cannot_render_takes_the_request_out_of_the_jsonp_path(
    base, admin_h, sheet_id, alt, message
):
    """Measured on Sheets: `alt=media` and `alt=zzz` answer their own 400 as `application/json` and
    unwrapped, even when `callback` is itself unparseable — so the `alt` refusal wins over the
    callback one, and an `alt` whose format the API cannot render suppresses the wrap."""
    for callback in ("cb", "a b"):
        r = _values(base, admin_h, sheet_id, "Sheet1!A1", alt=alt, callback=callback)
        assert r.status_code == 400, r.text
        assert r.headers["content-type"] == "application/json; charset=UTF-8"
        assert message in _gerr(r)["message"]


@pytest.mark.parametrize("path, code", JSONP_FAMILY_ERRORS)
@pytest.mark.parametrize("alt", ["json", "JSON", "Json", ""])
def test_an_alt_that_still_means_json_keeps_the_callback(client, path, code, alt):
    """`alt` is matched without regard to case, and an empty `alt=` names no format at all.

    Measured 2026-09-17, anonymous on all five families and authenticated on Sheets: `alt=JSON`,
    `alt=Json` and `alt=` each come back as the same 200 script `alt=json` does. Comparing the
    value literally answered all three at the error status with an unwrapped body, which is the one
    shape a page loading the answer through a `<script>` element cannot read."""
    r = client.get(path, params={"callback": "cb", "alt": alt})
    assert _jsonp(r, "cb")["error"]["code"] == code


@pytest.mark.parametrize("alt", ["json", "JSON", "Json", ""])
def test_the_alt_spellings_a_success_is_served_through(base, admin_h, sheet_id, alt):
    """The success half of the same measurement: authenticated on Sheets, each of these answers the
    200 a request naming no `alt` answers."""
    r = _values(base, admin_h, sheet_id, "Sheet1!A1", alt=alt)
    assert r.status_code == 200, r.text
    assert r.json()["range"] == "Sheet1!A1"


@pytest.mark.parametrize(
    "alt, message",
    [
        ("media", 'Unsupported alt type "media" for non byte stream request.'),
        ("MEDIA", 'Unsupported alt type "media" for non byte stream request.'),
        ("Media", 'Unsupported alt type "media" for non byte stream request.'),
        ("zzz", "Invalid value \"zzz\" for query parameter 'alt'"),
        ("ZZZ", "Invalid value \"ZZZ\" for query parameter 'alt'"),
    ],
)
def test_the_two_alt_refusals_quote_different_spellings(base, admin_h, sheet_id, alt, message):
    """A format real recognises but cannot render here is reported lowercased; one it does not
    recognise is quoted the way it arrived. Measured 2026-09-17 on Sheets with a credential, which
    is what it takes to reach either sentence — anonymous, the 403 comes first."""
    r = _values(base, admin_h, sheet_id, "Sheet1!A1", alt=alt)
    assert r.status_code == 400, r.text
    assert _gerr(r)["message"] == message


def test_a_repeated_callback_is_answered_through_the_first_one(base, admin_h, sheet_id):
    """`$.xgafv` is the system parameter real reads LAST; `callback` and `alt` it reads FIRST.
    Measured 2026-09-15: `cb&dd` calls `cb`, `dd&cb` calls `dd`, an empty first repeat is no
    callback however the second one reads, and a second repeat is not validated at all — `cb&a b`
    answers the success through `cb` rather than refusing the name."""
    url = f"{base}/sheets/v4/spreadsheets/{sheet_id}/values/Sheet1%21A1"
    assert httpx.get(f"{url}?callback=cb&callback=dd", headers=admin_h).text.startswith(
        "// API callback\ncb("
    )
    assert httpx.get(f"{url}?callback=dd&callback=cb", headers=admin_h).text.startswith(
        "// API callback\ndd("
    )
    empty_first = httpx.get(f"{url}?callback=&callback=cb", headers=admin_h)
    assert empty_first.headers["content-type"] == "application/json; charset=UTF-8"
    assert httpx.get(f"{url}?callback=cb&callback=", headers=admin_h).text.startswith(
        "// API callback\ncb("
    )
    bad_second = httpx.get(f"{url}?callback=cb&callback=a%20b", headers=admin_h)
    assert _jsonp(bad_second, "cb")["range"] == "Sheet1!A1"


def test_a_repeated_alt_decides_the_wrap_from_the_first_one(base, admin_h, sheet_id):
    """The same rule on the parameter that suppresses the wrap, and the reason `_sheets_respond`
    reads `alt` the way `gerr.jsonp_callback` does. Measured: `media&json` answers the `media`
    refusal unwrapped, `json&media` answers the success wrapped."""
    url = f"{base}/sheets/v4/spreadsheets/{sheet_id}/values/Sheet1%21A1"
    media_first = httpx.get(f"{url}?callback=cb&alt=media&alt=json", headers=admin_h)
    assert media_first.status_code == 400
    assert media_first.headers["content-type"] == "application/json; charset=UTF-8"
    assert "Unsupported alt type" in _gerr(media_first)["message"]
    json_first = httpx.get(f"{url}?callback=cb&alt=json&alt=media", headers=admin_h)
    assert _jsonp(json_first, "cb")["range"] == "Sheet1!A1"


def _compact(r):
    return r.status_code, not r.text.startswith("{\n")


def _mimes(r):
    return r.status_code, sorted({f["mimeType"] for f in r.json().get("files", [])})


def _names(r):
    return r.status_code, [f["name"] for f in r.json().get("files", [])]


def _ids(r):
    return r.status_code, [f["id"] for f in r.json().get("files", [])]


def _keys(r):
    return r.status_code, sorted(r.json())


def _values_of(r):
    return r.status_code, r.json().get("values")


def _grid(r):
    """The keys, and whether any sheet came back carrying cells: a mask that reaches the cells is
    what decides the grid, so a route that read one end for the grid and the other for the body
    answers the right keys without the cells."""
    sheets = r.json().get("sheets", [])
    return r.status_code, sorted(r.json()), any("data" in s for s in sheets)


_FILES = "/drive/v3/files"
_VALUES = "/sheets/v4/spreadsheets/{sid}/values/Sheet1!A1"
_BOOK = "/sheets/v4/spreadsheets/{sid}"
_BY_FILTER = "/sheets/v4/spreadsheets/{sid}:getByDataFilter"
_VALUES_BY_FILTER = "/sheets/v4/spreadsheets/{sid}/values:batchGetByDataFilter"
_BATCH_GET = "/sheets/v4/spreadsheets/{sid}/values:batchGet"
_BY_FILTER_BODY = {"dataFilters": [{"a1Range": "Sheet1!A1"}]}
_FOLDERS = "mimeType='application/vnd.google-apps.folder'"
_SPREADSHEETS = "mimeType='application/vnd.google-apps.spreadsheet'"
_CELLS = "sheets.data.rowData.values.formattedValue"

# The pairs `gerr.first_repeat` records, on each route Backlot reads the parameter on, and the empty
# first repeat and unvalidated second repeat it describes: (method, path, fixed params, parameter,
# one value, the other, the end real reads, what to compare). `callback`, `alt` and `$.xgafv` have
# tests of their own above, and `valueRenderOption` has rows in
# `test_the_render_options_differ_over_typed_cells`, since the bundled corpus states no typed cell
# for the options to render differently. `{sid}` is the spreadsheet, `{folder}` a folder and
# `{token}` a valid page token.
REPEATED = [
    ("GET", _FILES, {}, "fields", "files(id)", "bogus", "first", _keys),
    ("GET", _FILES + "/{sid}", {}, "fields", "id", "bogus", "first", _keys),
    ("GET", _FILES + "/{folder}", {}, "fields", "id", "bogus", "first", _keys),
    ("GET", "/drive/v3/about", {}, "fields", "user", "bogus", "first", _keys),
    ("GET", "/drive/v3/about", {}, "fields", "", "user", "first", _keys),
    ("GET", _VALUES, {}, "fields", "range", "bogus", "first", _keys),
    ("GET", _VALUES, {}, "fields", "", "range", "first", _keys),
    ("GET", _BOOK, {}, "fields", _CELLS, "spreadsheetId", "first", _grid),
    ("POST", _BY_FILTER, {}, "fields", _CELLS, "spreadsheetId", "first", _grid),
    (
        "GET",
        _BATCH_GET,
        {"ranges": "Sheet1!A1"},
        "fields",
        "spreadsheetId",
        "bogus",
        "first",
        _keys,
    ),
    ("POST", _BY_FILTER, {}, "fields", "spreadsheetId", "bogus", "first", _keys),
    ("POST", _VALUES_BY_FILTER, {}, "fields", "spreadsheetId", "bogus", "first", _keys),
    ("GET", _VALUES, {}, "prettyPrint", "false", "true", "first", _compact),
    ("GET", _VALUES, {}, "prettyPrint", "", "false", "first", _compact),
    ("GET", _BOOK, {}, "prettyPrint", "false", "true", "first", _compact),
    ("GET", _BATCH_GET, {"ranges": "Sheet1!A1"}, "prettyPrint", "false", "true", "first", _compact),
    ("POST", _BY_FILTER, {}, "prettyPrint", "false", "true", "first", _compact),
    ("POST", _VALUES_BY_FILTER, {}, "prettyPrint", "false", "true", "first", _compact),
    ("GET", _FILES, {}, "q", _FOLDERS, _SPREADSHEETS, "first", _mimes),
    ("GET", _FILES, {}, "q", "", _FOLDERS, "first", _mimes),
    ("GET", _FILES, {}, "q", _FOLDERS, "nosuchfield = 1", "first", _mimes),
    ("GET", _FILES, {}, "pageSize", "1", "3", "first", _ids),
    ("GET", _FILES, {"pageSize": "1"}, "pageToken", "{token}", "BOGUS", "first", _ids),
    ("GET", _FILES, {"pageSize": "1"}, "pageToken", "", "{token}", "first", _ids),
    ("GET", _FILES + "/{sid}/permissions", {}, "pageToken", "", "bad", "first", _keys),
    ("GET", "/drive/v3/drives", {}, "pageToken", "", "bad", "first", _keys),
    ("GET", _FILES, {"pageSize": "3"}, "orderBy", "name", "name desc", "first", _names),
    ("GET", _FILES, {"pageSize": "3"}, "orderBy", "name", "bogus", "first", _names),
    (
        "GET",
        _FILES + "/{sid}/export",
        {},
        "mimeType",
        "text/csv",
        "text/tab-separated-values",
        "first",
        lambda r: r.headers["content-type"],
    ),
    ("GET", _VALUES + ":B2", {}, "majorDimension", "ROWS", "COLUMNS", "last", _values_of),
    ("GET", _BOOK, {}, "includeGridData", "true", "false", "last", _grid),
]


@pytest.mark.parametrize(
    "method, path, fixed, name, first, second, reads, seen",
    REPEATED,
    ids=[f"{r[3]}-{r[4] or 'empty'}-then-{r[5] or 'empty'}-{r[1]}" for r in REPEATED],
)
def test_a_repeated_parameter_is_read_from_the_end_real_reads_it_from(
    base, admin_h, sheet_id, method, path, fixed, name, first, second, reads, seen
):
    """Each pair both ways round answers what the end real reads answers on its own, and the two
    values alone answer differently, so the row could tell the two ends apart."""
    listing = httpx.get(f"{base}{_FILES}", headers=admin_h, params={"pageSize": "1"}).json()
    folders = httpx.get(f"{base}{_FILES}", headers=admin_h, params={"q": _FOLDERS}).json()
    url = base + path.format(sid=sheet_id, folder=folders["files"][0]["id"])
    first, second = (v.format(token=listing["nextPageToken"]) for v in (first, second))
    body = _BY_FILTER_BODY if method == "POST" else None

    def send(*values):
        params = [*fixed.items(), *((name, v) for v in values)]
        return seen(httpx.request(method, url, headers=admin_h, params=params, json=body))

    alone = {v: send(v) for v in (first, second)}
    assert alone[first] != alone[second], alone
    end = 0 if reads == "first" else -1
    assert send(first, second) == alone[(first, second)[end]]
    assert send(second, first) == alone[(second, first)[end]]


def test_every_google_get_refuses_a_callback_it_cannot_call_and_no_post_reads_one(client):
    """The other half of
    ``test_every_google_operation_declares_the_system_parameters_and_reads_them`` for `callback`,
    split the way real splits it.

    Every family GET is sent a name that cannot be a JavaScript one and has to refuse it. Every
    family POST is sent the same name and has to IGNORE it: measured on Sheets, a `callback` on
    `values:batchGetByDataFilter` and on `spreadsheets:getByDataFilter` is not honoured and not even
    validated, where the same POST honours `$.xgafv` and `prettyPrint` — JSONP is what a `<script>`
    element fetches, and a `<script>` element issues a GET.

    `callback` is NOT declared router-wide beside `$.xgafv` — Sheets is the only family whose
    SUCCESS is wrapped, and `qp` declares only what Backlot honours — so this is the whole of what
    the document says about it.

    A byte-stream read is not JSONP and is pinned by the tests beside
    `test_a_callback_on_a_download_is_real_503_to_the_byte` instead, because the answer an
    uncallable name gets there is not this 400 but real's 503 `Backend Error` (measured 2026-10-04
    and 2026-10-07) -- which is what the anonymous case here asserts before the path leaves the
    sweep."""
    spec = client.get("/openapi.json").json()
    families = ("/drive/v3", "/gmail/v1", "/docs/v1", "/sheets/v4", "/slides/v1")
    gets, posts, wrong = 0, 0, []
    for path, item in spec["paths"].items():
        if not path.startswith(families):
            continue
        if path.endswith("/export"):
            anonymous = client.get(f"{path}?callback=a%20b")
            assert anonymous.status_code == 503, path
            assert "Backend Error" in anonymous.text, path
            continue
        for method, op in item.items():
            if method not in ("get", "post", "put", "patch", "delete"):
                continue
            url = re.sub(r"\{[^}]+\}", "dummy", path)
            r = client.request(method.upper(), f"{url}?callback=a%20b", json={})
            refused = r.status_code == 200 and "Invalid JSONP callback name" in r.text
            if method == "get":
                gets += 1
                if not refused:
                    wrong.append(f"GET {path} did not refuse -> {r.status_code}")
            else:
                posts += 1
                if refused:
                    wrong.append(f"{method.upper()} {path} refused where real ignores")
    assert wrong == []
    assert gets and posts, f"both halves have to have run, got {gets} GETs and {posts} POSTs"


def test_a_family_path_with_no_route_is_not_answered_by_calling_an_unchecked_name(client):
    """The sweep above reads the document, so the paths NOT in it are its blind spot — and those
    are exactly the ones ``gerr.validate_system_parameters`` never sees, because a router dependency
    runs only once a route has matched. ``gerr.rendered`` checks the name a second time for them.

    Real answers an unrouted family path from its front end as HTML — measured 2026-09-16 on all
    five, with a `callback` and without: 400 on Sheets, Docs and Slides and 404 on Drive and Gmail.
    So JSONP is not its shape there under ANY name, and a name that can be called is dropped with
    one that cannot rather than answered at 200 as a script."""
    for prefix in ("/sheets/v4", "/docs/v1", "/slides/v1", "/drive/v3", "/gmail/v1"):
        plain = client.get(f"{prefix}/nope")
        assert plain.status_code == 404, plain.text
        for name in ("a b", "cb"):
            r = client.get(f"{prefix}/nope", params={"callback": name})
            assert r.status_code == 404, (prefix, name, r.text)
            assert r.headers["content-type"] == "application/json; charset=UTF-8"
            assert "// API callback" not in r.text, (prefix, name)
            assert r.text == plain.text, (prefix, name)


def test_a_post_ignores_a_callback_the_way_real_does(base, admin_h, sheet_id):
    """The measured POST, not a synthesized one: `values:batchGetByDataFilter` answers a good
    `callback` with an unwrapped `application/json; charset=UTF-8` body and an unparseable one the
    same way, where the same route under `$.xgafv=9` still refuses the value."""
    url = f"{base}/sheets/v4/spreadsheets/{sheet_id}/values:batchGetByDataFilter"
    body = {"dataFilters": [{"a1Range": "Sheet1!A1"}]}
    for callback in ("cb", "a b"):
        r = httpx.post(url, headers=admin_h, params={"callback": callback}, json=body)
        assert r.status_code == 200, r.text
        assert r.headers["content-type"] == "application/json; charset=UTF-8"
        assert "// API callback" not in r.text
        assert r.json()["valueRanges"]
    refused = httpx.post(
        url, headers=admin_h, params={"callback": "a b", "$.xgafv": "9"}, json=body
    )
    assert refused.status_code == 400
    assert _gerr(refused)["message"] == XGAFV_REFUSAL.format("9")


# Every character real's serializer writes as an escape rather than as itself, as ranges. Measured
# 2026-09-17 by sending each of the 1,112,063 codepoints a query string carries -- every one but
# the surrogates and U+0000, which the front end hands back as the literal `%00` rather than
# decoding -- through the `alt` echo on Sheets in 1,013 requests, and reading which came back
# escaped. 209 do. Spelled out here rather than imported from `gerr`, so what the module produces
# is checked against the measurement and not against itself.
MEASURED_ESCAPES = (
    (0x0001, 0x001F),
    (0x0022, 0x0022),
    (0x003C, 0x003C),
    (0x003E, 0x003E),
    (0x005C, 0x005C),
    (0x007F, 0x009F),
    (0x00AD, 0x00AD),
    (0x0600, 0x0603),
    (0x06DD, 0x06DD),
    (0x070F, 0x070F),
    (0x17B4, 0x17B5),
    (0x200B, 0x200F),
    (0x2028, 0x202E),
    (0x2060, 0x2064),
    (0x206A, 0x206F),
    (0xFEFF, 0xFEFF),
    (0xFFF9, 0xFFFB),
    (0x1D173, 0x1D17A),
    (0xE0001, 0xE0001),
    (0xE0020, 0xE007F),
)

# Characters the rule is easy to guess wrong, all of which the sweep found raw: `&` and `'`, NBSP
# and U+3000, and five format characters. U+061C, U+0604 and U+180E are Cf today and are NOT
# escaped; U+110BD, U+13430 and U+1BCA0 are Cf and astral and are not either, where U+1D173 and
# U+E0001 are. Read with U+17B4 and U+17B5, which ARE escaped and have been Mn since Unicode 4.1,
# the set is that version's format category rather than any category a lookup would return today.
RAW_THROUGH_THE_SERIALIZER = (
    0x0026,
    0x0027,
    0x00A0,
    0x3000,
    0x061C,
    0x0604,
    0x0605,
    0x180E,
    0x110BD,
    0x13430,
    0x1BCA0,
)


def test_the_characters_the_serializer_escapes():
    """The sweep above, against the module that has to reproduce it.

    209 codepoints move and 1,111,854 do not. Reading the rule off a category test gets it wrong in
    both directions, which is why the ranges are data here."""
    escaped = {cp for lo, hi in MEASURED_ESCAPES for cp in range(lo, hi + 1)}
    assert len(escaped) == 209
    for cp in sorted(escaped):
        assert gerr._escaped_name(chr(cp)) != chr(cp), hex(cp)
    edges = {cp for lo, hi in MEASURED_ESCAPES for cp in (lo - 1, hi + 1)} - escaped
    for cp in sorted(edges.union(RAW_THROUGH_THE_SERIALIZER)):
        if cp < 0x0001 or 0xD800 <= cp <= 0xDFFF:
            continue
        assert gerr._escaped_name(chr(cp)) == chr(cp), hex(cp)


@pytest.mark.parametrize(
    "ch, written",
    [
        ("\t", "\\t"),
        ("\\", "\\\\"),
        ("\u007f", "\\u007f"),
        ("\u00ad", "\\u00ad"),
        ("\u200b", "\\u200b"),
        ("\u2028", "\\u2028"),
        ("\U0001d173", "\\ud834\\udd73"),
        ("\U000e0001", "\\udb40\\udc01"),
    ],
)
def test_a_callback_name_reaches_the_wrapper_escaped(client, admin_h, ch, written):
    """A name is not serialized JSON, so the wrapper carries more of the rule than a body does:
    nothing has escaped its quotes, backslashes and C0 controls before it gets there.

    These are the names real wrote into ``// API callback\\n…(`` for `cb<CH>x`, measured on Sheets
    on 2026-09-17, astral characters in the surrogate pair it spells them with. Every one of them
    is refused — the character set a name may be built from is ASCII letters, digits and `_$.[]` —
    and the refusal still arrives through the name it refuses, so this is what the client reads."""
    r = client.get("/sheets/v4/spreadsheets/x", headers=admin_h, params={"callback": f"cb{ch}x"})
    assert r.status_code == 200
    assert r.text.startswith(f"// API callback\ncb{written}x(")
    assert _jsonp(r, f"cb{ch}x")["error"]["message"] == CALLBACK_REFUSAL.format(f"cb{ch}x")


@pytest.mark.parametrize(
    "ch, written",
    [
        ("\u00ad", "\\u00ad"),
        ("\u007f", "\\u007f"),
        ("\u200b", "\\u200b"),
        ("\u2028", "\\u2028"),
        ("\U0001d173", "\\ud834\\udd73"),
    ],
)
def test_the_body_carries_the_escapes_the_wrapper_does(base, admin_h, sheet_id, ch, written):
    """The same rule on a plain body, where `json.dumps(…, ensure_ascii=False)` writes every one of
    these raw. Measured on Sheets: an unparseable range carrying one comes back as
    `"Unable to parse range: a\\u200bb!!"` and its kind."""
    r = _values(base, admin_h, sheet_id, f"a{ch}b!!")
    assert r.status_code == 400, r.text
    assert f"a{written}b!!" in r.text
    assert ch not in r.text
    assert _gerr(r)["message"] == f"Unable to parse range: a{ch}b!!"


@pytest.mark.parametrize("ch", ["　", " ", "&", "'", "\U0001bca0"])
def test_the_characters_a_body_keeps(base, admin_h, sheet_id, ch):
    """The other half of the same request: measured, `a<U+3000>b!!` comes back with the ideographic
    space itself in the message, and so do NBSP, `&`, `'` and an astral format character the rule
    leaves out."""
    r = _values(base, admin_h, sheet_id, f"a{ch}b!!")
    assert r.status_code == 400, r.text
    assert f"a{ch}b!!" in r.text


def test_angle_brackets_are_escaped_in_a_google_error(base, admin_h, sheet_id):
    """Measured on both sides of the same API: an error message echoing an unparseable range
    spelled `<b>&'x` comes back with the brackets escaped and `&` and `'` raw. The escape is what
    keeps a body from closing a `<script>` element around it, so it is on the plain body too."""
    r = _values(base, admin_h, sheet_id, "<b>&'x")
    assert "\\u003cb\\u003e&'x" in r.text
    assert "<b>" not in r.text
    assert _gerr(r)["message"] == "Unable to parse range: <b>&'x"


def test_angle_brackets_are_escaped_in_a_google_success(tmp_path):
    """The same serializer on the success side, measured the same way: a Sheets cell holding
    `<b>&'x` came back `\\u003cb\\u003e&'x` from the live API, indented, compact and wrapped
    alike."""
    from tests._helpers import corpus_client

    record = {
        "source_type": "google_drive",
        "doc_id": "angle",
        "folder": "mk",
        "title": "Angle probe",
        "author_email": "a@x.com",
        "visibility": "public",
        "subtype": "spreadsheet",
        "sheets": [{"title": "Sheet1", "grid": [["<b>&'x"]]}],
    }
    with corpus_client(tmp_path, [record]) as (client, settings):
        h = {"Authorization": f"Bearer {settings.admin_token}"}
        (sheet,) = client.get(
            "/drive/v3/files", headers=h, params={"q": "name = 'Angle probe'"}
        ).json()["files"]
        path = f"/sheets/v4/spreadsheets/{sheet['id']}/values/Sheet1!A1"
        for params in ({}, {"prettyPrint": "false"}, {"callback": "cb"}):
            r = client.get(path, headers=h, params=params)
            assert "\\u003cb\\u003e&'x" in r.text, params
            assert "<b>" not in r.text, params


def test_a_callback_changes_nothing_outside_google(client, admin_h):
    """The handler serves Atlassian and GitHub too, and a `callback` leaves each answering its own
    `JSONResponse`, each vendor's own charset (``errors.github.json_media_type``,
    ``errors.atlassian.json_media_type``) included. The Jira half asks a route that exists for a
    resource that does not, so what it pins is the shared envelope rather than the RFC 7807 shape a
    path with no route answers (``errors.atlassian.no_endpoint``).

    Measured 2026-09-15, that is right for both and for different reasons. Jira and Confluence
    ignore `callback` outright — a 404 and a 200 come back identical with and without it. GitHub
    honours it, but through an envelope of its own: `/**/cb({"meta": {…, "status": 404}, "data":
    <the body>})` at 200 under `application/javascript; charset=utf-8`, compact, with an
    unparseable name refused UNWRAPPED at 400 and an EMPTY one refused rather than ignored — the
    opposite of Google on both counts. Sharing Google's renderer would answer neither vendor's
    shape, so this test pins that it is not shared."""
    gh = client.get("/github/repos/nope/nope", headers=admin_h, params={"callback": "cb"})
    assert gh.status_code == 404
    assert gh.headers["content-type"] == "application/json; charset=utf-8"
    assert gh.text == json.dumps(gh.json(), separators=(",", ":"))
    jira = client.get(
        "/atlassian/rest/api/3/issue/NOPE-1", headers=admin_h, params={"callback": "cb"}
    )
    assert jira.status_code == 404
    assert jira.headers["content-type"] == "application/json;charset=UTF-8"
    assert jira.text == json.dumps(jira.json(), separators=(",", ":"))


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
    # permissions.list has to resolve the same served id, for an actual FILE and not only for the
    # folder id `test_drive_folder_permissions_resolve` covers -- both routes
    # resolve through store.gdrive_by_id now, and only the folder path was exercised before.
    perms = client.get(f"/drive/v3/files/{doc['id']}/permissions", headers=admin_h)
    assert perms.status_code == 200


def test_drive_export_and_media_stay_non_json(client, admin_h):
    # A native doc exports as a raw Response; response_model must NOT be attached to these.
    doc = _drive_find(client, admin_h, "Brand")
    exp = client.get(
        f"/drive/v3/files/{doc['id']}/export", params={"mimeType": "text/plain"}, headers=admin_h
    )
    assert exp.status_code == 200 and "application/json" not in exp.headers["content-type"]
    # A binary (pdf) downloads raw via alt=media.
    pdf = _drive_find(client, admin_h, "Whitepaper")
    med = client.get(f"/drive/v3/files/{pdf['id']}", params={"alt": "media"}, headers=admin_h)
    assert med.status_code == 200 and "application/json" not in med.headers["content-type"]


# Real's 503 body for a download carrying a `callback`, to the byte: measured 2026-10-07 over the
# thirteen request shapes that reach one, the `errors[]` entry is written INLINE and the body ends
# with the closing brace and no trailing newline -- which is not the shape `respond`, the serializer
# every other Google body goes through, writes for that envelope.
DOWNLOAD_503_BODY = (
    "{\n"
    '  "error": {\n'
    '    "code": 503,\n'
    '    "message": "Backend Error",\n'
    '    "errors": [{\n'
    '      "message": "Backend Error",\n'
    '      "domain": "global",\n'
    '      "reason": "backendError"\n'
    "    }]\n"
    "  }\n"
    "}"
)
DOWNLOAD_503_TYPE = "text/javascript; charset=UTF-8"
MISSING_API_KEY = "The request is missing a valid API key."


def _download_requests(client, admin_h):
    """The two byte-stream reads of the bundled corpus, and the metadata read of the same PDF."""
    pdf = _drive_find(client, admin_h, "Whitepaper")["id"]
    doc = _drive_find(client, admin_h, "Brand")["id"]
    return {
        "media": (f"/drive/v3/files/{pdf}", {"alt": "media"}),
        "export": (f"/drive/v3/files/{doc}/export", {"mimeType": "text/plain"}),
        "metadata": (f"/drive/v3/files/{pdf}", {}),
        "doc": doc,
    }


def test_a_download_names_the_missing_api_key_where_the_metadata_read_does_not(client, admin_h):
    """Measured 2026-10-04 on `files.export` and 2026-10-05 on `files.get?alt=media`: a byte-stream
    read with no `Authorization` answers real's missing-API-key sentence with `reason: forbidden`
    and no `status`, where the metadata read of the same file answers the unregistered caller."""
    requests = _download_requests(client, admin_h)
    for kind in ("media", "export"):
        url, params = requests[kind]
        refusal = client.get(url, params=params)
        assert refusal.status_code == 403, url
        error = refusal.json()["error"]
        assert error["message"] == MISSING_API_KEY, url
        assert error["errors"][0]["reason"] == "forbidden", url
        assert "status" not in error, url
    metadata_url, metadata_params = requests["metadata"]
    metadata = client.get(metadata_url, params=metadata_params)
    assert metadata.json()["error"]["status"] == "PERMISSION_DENIED"


def test_a_download_answers_its_parameters_before_a_missing_credential(client, admin_h):
    """Measured 2026-10-07: without a credential a download answers an absent `mimeType` and a
    mistyped `supportsAllDrives` with their 400, where the metadata read of the same file answers
    the unregistered caller first. Together with the `callback` 503 ahead of them and the missing
    API key after, that is the whole measured order a byte-stream read answers in. Beside a callable
    `callback` each of the two is the download's 503 instead, credential or not."""
    pdf = _drive_find(client, admin_h, "Whitepaper")["id"]
    doc = _drive_find(client, admin_h, "Brand")["id"]
    bad_param = (f"/drive/v3/files/{pdf}", {"alt": "media", "supportsAllDrives": "NOPE"})
    no_mime = (f"/drive/v3/files/{doc}/export", {})
    for url, params in (bad_param, no_mime):
        anonymous = client.get(url, params=params)
        assert anonymous.status_code == 400, (url, anonymous.text)
        assert client.get(url, params=params, headers=admin_h).status_code == 400, url
        assert client.get(url, params={**params, "callback": "cb"}).status_code == 503, url
    assert "TYPE_BOOL" in client.get(bad_param[0], params=bad_param[1]).text
    assert "mimeType" in client.get(no_mime[0], params=no_mime[1]).text
    metadata = client.get(f"/drive/v3/files/{pdf}", params={"supportsAllDrives": "NOPE"})
    assert metadata.status_code == 403
    assert metadata.json()["error"]["status"] == "PERMISSION_DENIED"


def test_a_callback_on_a_download_is_real_503_to_the_byte(client, admin_h):
    """Measured 2026-10-07: a `callback` that cannot be called on a byte-stream read is 503
    `Backend Error` under the script's type, unwrapped, and the body is one fixed string -- the
    `errors[]` entry inline and no trailing newline, where the serializer every other Google body
    goes through writes the array expanded and ends with one.

    The same request with no `Authorization` answers it too: the callback is refused ahead of the
    missing-API-key 403, which is the order real answers them in."""
    requests = _download_requests(client, admin_h)
    for kind in ("media", "export"):
        url, params = requests[kind]
        for headers in (admin_h, {}):
            refusal = client.get(url, params={**params, "callback": "a b"}, headers=headers)
            assert refusal.status_code == 503, (kind, headers)
            assert refusal.headers["content-type"] == DOWNLOAD_503_TYPE, kind
            assert refusal.text == DOWNLOAD_503_BODY, kind
    # the exception the route raises still describes that body, so the signal and the literal
    # cannot drift apart
    assert json.loads(DOWNLOAD_503_BODY) == gerr.http_body(
        "/drive/v3/files/x", gerr.backend_error()
    )


def test_a_callback_on_a_download_turns_every_later_error_into_the_same_503(client, admin_h):
    """Measured 2026-10-07: on a byte-stream read carrying a name a script can call, EVERY error is
    the same 503 `Backend Error` -- a missing id, a Docs file read with `alt=media`, a mistyped
    `supportsAllDrives`, an absent `mimeType` and no credential alike -- where each of those without
    the callback keeps its own status. Only a success lets the name through."""
    pdf = _drive_find(client, admin_h, "Whitepaper")["id"]
    doc = _drive_find(client, admin_h, "Brand")["id"]
    for url, params, headers in (
        (f"/drive/v3/files/{pdf}", {"alt": "media", "supportsAllDrives": "NOPE"}, admin_h),
        ("/drive/v3/files/nosuch", {"alt": "media"}, admin_h),
        (f"/drive/v3/files/{doc}", {"alt": "media"}, admin_h),
        (f"/drive/v3/files/{doc}/export", {}, admin_h),
        (f"/drive/v3/files/{pdf}", {"alt": "media"}, {}),
    ):
        plain = client.get(url, params=params, headers=headers)
        assert plain.status_code != 503, (url, plain.text)
        refusal = client.get(url, params={**params, "callback": "cb"}, headers=headers)
        assert refusal.status_code == 503, (url, refusal.text)
        assert refusal.text == DOWNLOAD_503_BODY, (url, refusal.text)


def test_the_refusal_order_on_a_download_is_system_bearer_then_callback(client, admin_h):
    """On both downloads, `$.xgafv` 400 first, then a credential that does not resolve 401, then
    the callback's 503 -- the order `gerr.missing_api_key` records. Only a header `_credential`
    reads as a credential is one at that layer (`_require_download_bearer`): every other value
    below reaches the callback's 503 beside `callback=a b`. Each of them is a 401 without one, the
    front end's for a credential and the download server's (`gerr.download_credential_refused`)
    for the rest."""
    requests = _download_requests(client, admin_h)
    for kind in ("media", "export"):
        url, params = requests[kind]
        bad = {**params, "callback": "a b"}
        xgafv = client.get(url, params={**bad, "$.xgafv": "9"}, headers=BAD_TOKEN)
        assert xgafv.status_code == 400 and XGAFV_REFUSAL.format("9") in xgafv.text, kind
        for value, beside_callback in (
            ("Bearer not-a-real-token", 401),
            ("OAuth not-a-real-token", 401),
            ("Bearer not-a-real-token x", 401),
            ("Bearernot-a-real-token", 401),
            ("Bearer\tnot-a-real-token", 401),
            ("bearer not-a-real-token", 503),
            ("BEARER not-a-real-token", 503),
            ("token not-a-real-token", 503),
            ("Bearer", 503),
            ("bearer", 503),
            ("nope", 503),
            ("Basic YWJjOmRlZg==", 503),
            ("", 503),
        ):
            headers = {"Authorization": value}
            answer = client.get(url, params=bad, headers=headers)
            assert answer.status_code == beside_callback, (kind, value)
            if beside_callback == 503:
                assert answer.text == DOWNLOAD_503_BODY, (kind, value)
            else:
                assert answer.json()["error"]["status"] == "UNAUTHENTICATED", (kind, value)
            alone = client.get(url, params=params, headers=headers)
            assert alone.status_code == 401, (kind, value)
            if beside_callback == 503:
                assert (
                    _gerr(alone) == gerr.http_body(url, gerr.download_credential_refused())["error"]
                ), (kind, value)
                assert (
                    alone.headers["www-authenticate"]
                    == 'Bearer realm="https://accounts.google.com"'
                )
            else:
                assert _gerr(alone)["status"] == "UNAUTHENTICATED", (kind, value)


# A read per row whose query names a token, a byte-stream read but for `json`, as
# ``gerr.token_in_query`` records: the read (`media`; `export`; `bare`, an export with no
# `mimeType`; `missing`, an export of a file that does not exist; `json`, an export asking for
# `alt=json`), the query, the `Authorization`, and the status real answered with what identifies the
# answer: the message, or the challenge of a 401. The `media` row with `nope` is the answer real
# gave inside a batch (``gerr.token_in_query``).
_QUERY_TOKEN = gerr.QUERY_TOKEN_MESSAGE
# fmt: off
_DOWNLOAD_QUERY_TOKEN_ROWS = [
    ("export", "access_token=nope", None, 400, _QUERY_TOKEN),
    ("export", "access_token=", None, 400, _QUERY_TOKEN),
    ("export", "oauth_token=nope", None, 400, _QUERY_TOKEN),
    ("export", "ACCESS_TOKEN=nope", None, 400, _QUERY_TOKEN),
    ("export", "BEARER_TOKEN=nope", None, 400, _QUERY_TOKEN),
    ("export", "access_token=nope", "Bearer {t}", 400, _QUERY_TOKEN),
    ("export", "access_token=nope", "bearer nope", 400, _QUERY_TOKEN),
    ("export", "access_token=", "", 400, _QUERY_TOKEN),
    ("export", "access_token=nope&callback=a%20b", None, 400, _QUERY_TOKEN),
    ("export", "access_token=nope&callback=cb", None, 400, _QUERY_TOKEN),
    ("bare", "access_token=nope", None, 400, _QUERY_TOKEN),
    ("missing", "access_token=nope", None, 400, _QUERY_TOKEN),
    ("media", "access_token=", None, 400, _QUERY_TOKEN),
    ("media", "oauth_token=", None, 400, _QUERY_TOKEN),
    ("media", "ACCESS_TOKEN=nope", None, 400, _QUERY_TOKEN),
    ("media", "access_token=", "Bearer {t}", 400, _QUERY_TOKEN),
    ("media", "access_token=&callback=a%20b", None, 400, _QUERY_TOKEN),
    ("media", "access_token=nope", None, 400, _QUERY_TOKEN),
    # what comes before it
    ("export", "access_token=nope&%24.xgafv=9", None, 400, XGAFV_REFUSAL.format("9")),
    ("media", "access_token=&%24.xgafv=9", None, 400, XGAFV_REFUSAL.format("9")),
    ("export", "access_token=nope", "Bearer nope", 401, _CHALLENGED),
    ("media", "access_token=", "Bearer nope", 401, _CHALLENGED),
    ("export", "access_token=ya29.x", None, 401, _CHALLENGED),
    ("export", "access_token=nope", "Bearer{t}", 401, _CHALLENGED),
    # names it does not take, and a read that is no download
    ("export", "accesstoken=nope", None, 403, gerr.MISSING_API_KEY_MESSAGE),
    ("export", "id_token=nope", None, 403, gerr.MISSING_API_KEY_MESSAGE),
    ("json", "access_token=nope", None, 403, gerr.UNREGISTERED_CALLER_MESSAGE),
]
# fmt: on


@pytest.mark.parametrize("kind, query, value, status, answer", _DOWNLOAD_QUERY_TOKEN_ROWS)
def test_a_download_refuses_a_token_in_its_query(
    client, admin_h, tokens_yaml, kind, query, value, status, answer
):
    """The rows `_DOWNLOAD_QUERY_TOKEN_ROWS` records. A function of its own beside
    `test_the_refusal_order_on_a_download_is_system_bearer_then_callback`, whose rows are
    `Authorization` values on a query that names no token."""
    requests = _download_requests(client, admin_h)
    url, params = {
        "media": requests["media"],
        "export": requests["export"],
        "bare": (requests["export"][0], {}),
        "missing": ("/drive/v3/files/nosuchfile000/export", requests["export"][1]),
        "json": (requests["export"][0], {**requests["export"][1], "alt": "json"}),
    }[kind]
    sent = "&".join(f"{k}={v}" for k, v in params.items())
    token = tokens_yaml["admin_token"]
    headers = {} if value is None else {"Authorization": value.format(t=token)}
    r = client.get(f"{url}?{sent}&{query}" if sent else f"{url}?{query}", headers=headers)
    assert r.status_code == status, r.text
    if status == 401:
        assert r.headers["www-authenticate"] == answer
    else:
        assert _gerr(r)["message"] == answer


def test_a_download_is_the_route_not_the_path_shape(client, admin_h):
    """Measured 2026-10-07: an export asking for `alt=json` is not a download but an ordinary read
    -- the unregistered-caller 403 without a credential, the wrapped `callback` 400 with one -- and
    a file whose id is literally `export` is a `files.get`, answering the metadata read."""
    doc = _drive_find(client, admin_h, "Brand")["id"]
    anonymous = client.get(f"/drive/v3/files/{doc}/export", params={"mimeType": "x", "alt": "json"})
    assert anonymous.status_code == 403
    assert anonymous.json()["error"]["status"] == "PERMISSION_DENIED"
    wrapped = client.get(
        f"/drive/v3/files/{doc}/export",
        params={"mimeType": "x", "alt": "json", "callback": "a b"},
        headers=admin_h,
    )
    assert wrapped.status_code == 200 and "Invalid JSONP callback name" in wrapped.text
    literal = client.get("/drive/v3/files/export")
    assert literal.status_code == 403
    assert literal.json()["error"]["status"] == "PERMISSION_DENIED"
    cb = client.get("/drive/v3/files/export", params={"callback": "a b"}, headers=admin_h)
    assert cb.status_code == 200 and "Invalid JSONP callback name" in cb.text


def test_a_download_reads_the_first_callback_repeat_and_an_empty_one_is_none(client, admin_h):
    """Measured 2026-10-07: `cb&a b` on a download answers the bytes where `a b&cb` answers the 503,
    so the first repeat decides as it does for every other `callback`. An empty `callback=` is no
    callback at all: the download answers the plain 403."""
    pdf = _drive_find(client, admin_h, "Whitepaper")["id"]
    url = f"/drive/v3/files/{pdf}"
    raw = client.get(url, params={"alt": "media"}, headers=admin_h)
    first = client.get(f"{url}?alt=media&callback=cb&callback=a%20b", headers=admin_h)
    assert first.status_code == 200 and first.content == raw.content
    second = client.get(f"{url}?alt=media&callback=a%20b&callback=cb", headers=admin_h)
    assert second.status_code == 503 and second.text == DOWNLOAD_503_BODY
    assert client.get(f"{url}?alt=media&callback=", headers=admin_h).content == raw.content
    anonymous = client.get(f"{url}?alt=media&callback=")
    assert anonymous.status_code == 403
    assert anonymous.json()["error"]["message"] == MISSING_API_KEY
    # ...and the case of `alt` does not decide either: `alt=MEDIA` is the same download, and the
    # same callback 503, as `alt=media` (measured 2026-09-17 and 2026-10-07)
    upper = client.get(f"{url}?alt=MEDIA&callback=a%20b", headers=admin_h)
    assert upper.status_code == 503 and upper.text == DOWNLOAD_503_BODY


def test_the_alt_that_downloads_is_read_the_way_every_other_alt_is(client, admin_h, tokens):
    """Case does not decide a download and neither does the last repeat.

    Measured 2026-09-17 against `www.googleapis.com/drive/v3/files/<id>` with a credential:
    `alt=MEDIA` and `alt=Media` hand back the same bytes `alt=media` does, `alt=media&alt=json`
    downloads and `alt=json&alt=media` answers the metadata. Reading the parameter off
    ``QueryParams.get`` gave the opposite answer on the repeat and the metadata on both spellings,
    so a client that upper-cased the value got JSON where real gave it a file."""
    url = f"/drive/v3/files/{_drive_find(client, admin_h, 'Whitepaper')['id']}"
    raw = client.get(url, params={"alt": "media"}, headers=admin_h)
    assert raw.status_code == 200
    for spelling in ("MEDIA", "Media"):
        r = client.get(url, params={"alt": spelling}, headers=admin_h)
        assert (r.status_code, r.content) == (200, raw.content), spelling
    assert client.get(f"{url}?alt=media&alt=json", headers=admin_h).content == raw.content
    metadata = client.get(f"{url}?alt=json&alt=media", headers=admin_h)
    assert metadata.headers["content-type"].startswith("application/json")
    # The spelling reaches corpus content, so it has to reach the same ACL. A file only the admin
    # can see answers the scoped token 404 through `MEDIA` exactly as through `media`, where the
    # admin gets the 403 a native document's download is refused with — the visibility test runs
    # before the branch this reads `alt` in, and upper-casing the value does not step around it.
    restricted = _drive_find(client, admin_h, "Q1 Revenue Model")["id"]
    scoped = {"Authorization": f"Bearer {tokens['mia@acme.com']}"}
    for spelling in ("media", "MEDIA"):
        params = {"alt": spelling}
        assert (
            client.get(f"/drive/v3/files/{restricted}", params=params, headers=scoped).status_code
            == 404
        )
        assert (
            client.get(f"/drive/v3/files/{restricted}", params=params, headers=admin_h).status_code
            == 403
        )


# --- Drive fidelity: measured divergences from real Google Drive ---------------
#
# Each case below was diffed against https://www.googleapis.com/drive/v3 with equivalent
# credentials; Backlot's old behaviour returned 200 with wrong/unfiltered data, so a consumer
# could not tell anything was off.

FOLDER_MIME = "application/vnd.google-apps.folder"
DOC_MIME = "application/vnd.google-apps.document"


def _drive_ids(client, headers, **params):
    j = client.get("/drive/v3/files", headers=headers, params=params).json()
    return [f["id"] for f in j.get("files", [])]


def test_drive_shared_with_me_partitions_by_owner(client, tokens_yaml):
    """`q=sharedWithMe=true` returns only the items someone else shared with the caller, and
    `'me' in owners` the caller's own, so the two partition what the caller sees. Real refuses
    `sharedWithMe = false`, as `_FILES_Q` records, so the caller's own are asked for by owner."""
    mia = {"Authorization": f"Bearer {tok(tokens_yaml, 'mia@acme.com')}"}
    all_ids = set(_drive_ids(client, mia, q="trashed=false", pageSize=100))
    shared = set(_drive_ids(client, mia, q="sharedWithMe=true and trashed=false", pageSize=100))
    own = set(_drive_ids(client, mia, q="'me' in owners and trashed=false", pageSize=100))
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
        params={"q": "'me' in owners and trashed=false", "pageSize": 100},
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


@pytest.mark.parametrize(
    "order_by, listed",
    [
        ("sharedWithMeTime", "a2 a1 m1 m2"),
        ("sharedWithMeTime asc", "a2 a1 m1 m2"),
        ("sharedWithMeTime desc", "m2 m1 a2 a1"),
        ("modifiedByMeTime", "m2 m1 a1 a2"),
        ("modifiedByMeTime asc", "m2 m1 a1 a2"),
        ("modifiedByMeTime desc", "a2 a1 m2 m1"),
        ("viewedByMeTime", "a2 a1 m2 m1"),
        ("viewedByMeTime asc", "a2 a1 m2 m1"),
        ("viewedByMeTime desc", "m2 m1 a2 a1"),
        ("recency", "a2 a1 m2 m1"),
        ("recency asc", "a2 a1 m2 m1"),
        ("recency desc", "a2 a1 m2 m1"),
    ],
)
def test_drive_order_by_a_per_caller_time(tmp_path, order_by, listed):
    """Ava owns two of the files and the other two are Mia's, shared with her, modified in turn,
    so each block's order is its own and not the files' modification order. None is viewed, so
    under `viewedByMeTime` Mia's files sort after Ava's own, where `_DRIVE_ORDER_KEYS` says real
    puts them."""
    records = [
        {
            "source_type": "google_drive",
            "doc_id": doc_id,
            "folder": "f",
            "title": doc_id,
            "content": "p",
            "author_email": f"{owner}@acme.com",
            "readers": ["ava@acme.com", "mia@acme.com"],
            "visibility": "private",
            "created": f"2026-01-{day:02d}T09:00:00Z",
            "updated": f"2026-01-{day:02d}T09:00:00Z",
        }
        for doc_id, owner, day in (
            ("a1", "ava", 1),
            ("m1", "mia", 5),
            ("a2", "ava", 10),
            ("m2", "mia", 15),
        )
    ]
    s = tiny_corpus(tmp_path, records)
    ava = tok(yaml.safe_load(s.tokens_path.read_text(encoding="utf-8")), "ava@acme.com")
    with client_for(s, reload=True) as c:
        r = c.get(
            "/drive/v3/files",
            headers={"Authorization": f"Bearer {ava}"},
            params={
                "orderBy": order_by,
                "fields": "files(name)",
                "q": "mimeType != 'application/vnd.google-apps.folder'",
            },
        )
    assert r.status_code == 200, r.text
    assert [f["name"] for f in r.json()["files"]] == listed.split()


# Each set of `capabilities` true for a file or a folder, as its owner and as a reader read it
# (`_drive_file_capabilities`); every other one is false.
_CAPS_TRUE = {
    ("file", True): {
        "canDownload",
        "canComment",
        "canEdit",
        "canCopy",
        "canShare",
        "canRename",
        "canTrash",
        "canDelete",
        "canReadRevisions",
        "canModifyContent",
    },
    ("file", False): {"canDownload", "canCopy"},
    ("folder", True): {
        "canDownload",
        "canListChildren",
        "canComment",
        "canEdit",
        "canShare",
        "canRename",
        "canTrash",
        "canDelete",
        "canAddChildren",
        "canModifyContent",
    },
    ("folder", False): {"canDownload", "canListChildren"},
}


@pytest.mark.parametrize(
    "name, owned, kind",
    [
        ("Mine", True, "file"),
        ("Theirs", False, "file"),
        ("Plans", False, "folder"),
        ("Figures", True, "folder"),
        ("Drafts", False, "folder"),
    ],
)
def test_drive_owned_by_me_can_share_and_modified_by_me_follow_the_caller(
    tmp_path, name, owned, kind
):
    """`ownedByMe` is per caller, and `canShare` and `modifiedByMe` with it: what the caller owns it
    can share, and what it can only read it cannot; `_modified_by_me` says why the caller modified
    exactly what it owns. Real answered `canShare: true` on 117 of the 118 items a Google Workspace
    member's `files.list` named as its own on 2026-10-18 and on 2026-10-19, and `false` on each
    folder and each file shared with it to read. `Plans` is a folder a record's `folder` names, and
    `Figures` and `Drafts` are `subtype: folder` records. The rest of `capabilities` is
    `_CAPS_TRUE`'s."""
    record = {
        "source_type": "google_drive",
        "folder": "Plans",
        "content": "body",
        "readers": ["ava@acme.com", "mia@acme.com"],
        "visibility": "private",
    }
    rows = [("mine", "Mine", "ava", None), ("theirs", "Theirs", "mia", None)]
    rows += [("figures", "Figures", "ava", "folder"), ("drafts", "Drafts", "mia", "folder")]
    s = tiny_corpus(
        tmp_path,
        [
            {
                **record,
                "doc_id": doc_id,
                "title": title,
                "author_email": f"{author}@acme.com",
                **({"subtype": subtype} if subtype else {}),
            }
            for doc_id, title, author, subtype in rows
        ],
    )
    tokens = yaml.safe_load(s.tokens_path.read_text(encoding="utf-8"))
    ava = {"Authorization": f"Bearer {tok(tokens, 'ava@acme.com')}"}
    with client_for(s, reload=True) as c:
        listed = {
            f["name"]: f
            for f in c.get(
                "/drive/v3/files",
                headers=ava,
                params={"fields": f"files(id,name,{_PER_CALLER})"},
            ).json()["files"]
        }[name]
        got = c.get(
            f"/drive/v3/files/{listed['id']}", headers=ava, params={"fields": _PER_CALLER}
        ).json()
    for f in (listed, got):
        assert (f["ownedByMe"], f["capabilities"]["canShare"], f["modifiedByMe"]) == (owned,) * 3
        assert f.get("modifiedByMeTime") == (f["modifiedTime"] if owned else None)
        assert {k for k, v in f["capabilities"].items() if v} == _CAPS_TRUE[kind, owned]


_PER_CALLER = "ownedByMe,capabilities,modifiedByMe,modifiedByMeTime,modifiedTime"


def test_drive_order_by_sorts_the_result(client, admin_h):
    """`orderBy` was accepted and never applied — silent, so a client that relies on server-side
    ordering appears to work against Backlot and misbehaves against production."""
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


def _nfd(name: str) -> str:
    return unicodedata.normalize("NFD", name)


# The first folder `backlot.collation` describes: its names in the order they were made, and the
# orders real listed them in.
_MADE = [
    "resume",
    "résumé",
    "resumé",
    "Resume",
    "RESUME",
    "cote",
    "coté",
    "côte",
    "côté",
    "가",
    "家",
    "可",
    "加",
    "각",
    "각2",
    "家2",
    "a",
    "A",
    "b",
    "x2",
    "x02",
    "x002",
    "x10",
    "x1٠",
    "x٢",
    "x²",
    "x2①",
    "x2²",
    "x20",
    "x 2",
    "x-2",
    "x_2",
    "x.2",
    "file99999999999999999999",
    "file100000000000000000000",
    "v1",
    "v01",
    "_a",
    "-a",
    ".a",
    "(a",
    "[a",
    "1a",
    "$a",
    "#a",
    "~a",
    "€a",
    "가나",
    "abc",
    "家族",
    "ガ",
    "α",
    "Ж",
    "😀",
    _nfd("한글"),
    "한글",
    _nfd("éclair"),
    "éclair",
]
_BY_NAME_NATURAL = [
    "_a",
    "-a",
    ".a",
    "(a",
    "[a",
    "#a",
    "~a",
    "😀",
    "$a",
    "€a",
    "1a",
    "A",
    "a",
    "abc",
    "b",
    "cote",
    "coté",
    "côte",
    "côté",
    "éclair",
    _nfd("éclair"),
    "file99999999999999999999",
    "file100000000000000000000",
    "RESUME",
    "Resume",
    "resume",
    "resumé",
    "résumé",
    "v01",
    "v1",
    "x 2",
    "x_2",
    "x-2",
    "x.2",
    "x٢",
    "x002",
    "x02",
    "x2",
    "x2①",
    "x2²",
    "x1٠",
    "x10",
    "x20",
    "x²",
    "α",
    "Ж",
    "가",
    "가나",
    "각",
    "각2",
    "한글",
    _nfd("한글"),
    "ガ",
    "加",
    "可",
    "家",
    "家2",
    "家族",
]
_BY_NAME_NATURAL_DESC = [
    "家族",
    "家2",
    "家",
    "可",
    "加",
    "ガ",
    "한글",
    _nfd("한글"),
    "각2",
    "각",
    "가나",
    "가",
    "Ж",
    "α",
    "x²",
    "x20",
    "x1٠",
    "x10",
    "x2²",
    "x2①",
    "x٢",
    "x002",
    "x02",
    "x2",
    "x.2",
    "x-2",
    "x_2",
    "x 2",
    "v01",
    "v1",
    "résumé",
    "resumé",
    "RESUME",
    "Resume",
    "resume",
    "file100000000000000000000",
    "file99999999999999999999",
    "éclair",
    _nfd("éclair"),
    "côté",
    "côte",
    "coté",
    "cote",
    "b",
    "abc",
    "A",
    "a",
    "1a",
    "€a",
    "$a",
    "😀",
    "~a",
    "#a",
    "[a",
    "(a",
    ".a",
    "-a",
    "_a",
]
_BY_NAME = [
    "#a",
    "$a",
    "(a",
    "-a",
    ".a",
    "1a",
    "[a",
    "_a",
    "A",
    "a",
    "abc",
    "b",
    "cote",
    "coté",
    "côte",
    "côté",
    _nfd("éclair"),
    "file100000000000000000000",
    "file99999999999999999999",
    "RESUME",
    "Resume",
    "resume",
    "resumé",
    "résumé",
    "v01",
    "v1",
    "x 2",
    "x-2",
    "x.2",
    "x002",
    "x02",
    "x10",
    "x1٠",
    "x2",
    "x20",
    "x2²",
    "x2①",
    "x_2",
    "x²",
    "x٢",
    "~a",
    "éclair",
    "α",
    "Ж",
    _nfd("한글"),
    "€a",
    "ガ",
    "加",
    "可",
    "家",
    "家2",
    "家族",
    "가",
    "가나",
    "각",
    "각2",
    "한글",
    "😀",
]
# The second: names some code points of which collate as one, runs of more digits than ICU reads
# as one number, and code points ICU weighs by their value.
_MADE_2 = [
    "la",
    "l\u00b7a",
    "lb",
    "l.a",
    _nfd("й"),
    "й",
    "ий",
    "ик",
    "и",
    "เก",
    "ก",
    "กา",
    "ข",
    "ཀ\u0fb2\u0f71\u0f80",
    "ཀ\u0fb2\u0f71",
    "ཀ\u0fb2",
    "ཀ\u0f71\u0f80",
    "9" * 254 + "1",
    "9" * 255,
    "1" + "0" * 254,
    "x𝟐",
    "x2",
    "x𝟐3",
    "x23",
    "x１０",
    "x11",
    "一",
    "𠀀",
    "a\u0378",
    "a\ue000",
    "a\U0010fffd",
    "a",
]
_BY_NAME_NATURAL_2 = [
    "1" + "0" * 254,
    "9" * 254 + "1",
    "9" * 255,
    "a",
    "a\u0378",
    "a\ue000",
    "a\U0010fffd",
    "l.a",
    "la",
    "l\u00b7a",
    "lb",
    "x2",
    "x𝟐",
    "x１０",
    "x11",
    "x23",
    "x𝟐3",
    "и",
    "ий",
    "ик",
    "й",
    _nfd("й"),
    "ก",
    "กา",
    "เก",
    "ข",
    "ཀ\u0fb2",
    "ཀ\u0fb2\u0f71",
    "ཀ\u0f71\u0f80",
    "ཀ\u0fb2\u0f71\u0f80",
    "一",
    "𠀀",
]
_BY_NAME_NATURAL_DESC_2 = [
    "𠀀",
    "一",
    "ཀ\u0fb2\u0f71\u0f80",
    "ཀ\u0f71\u0f80",
    "ཀ\u0fb2\u0f71",
    "ཀ\u0fb2",
    "ข",
    "เก",
    "กา",
    "ก",
    "й",
    _nfd("й"),
    "ик",
    "ий",
    "и",
    "x23",
    "x𝟐3",
    "x11",
    "x１０",
    "x2",
    "x𝟐",
    "lb",
    "l\u00b7a",
    "la",
    "l.a",
    "a\U0010fffd",
    "a\ue000",
    "a\u0378",
    "a",
    "9" * 255,
    "9" * 254 + "1",
    "1" + "0" * 254,
]
_BY_NAME_2 = [
    "1" + "0" * 254,
    "9" * 254 + "1",
    "9" * 255,
    "a",
    "a\u0378",
    "a\ue000",
    "a\U0010fffd",
    "l.a",
    "la",
    "lb",
    "l\u00b7a",
    "x11",
    "x2",
    "x23",
    "x１０",
    "x𝟐",
    "x𝟐3",
    "и",
    _nfd("й"),
    "ий",
    "ик",
    "й",
    "ก",
    "กา",
    "ข",
    "เก",
    "ཀ\u0f71\u0f80",
    "ཀ\u0fb2",
    "ཀ\u0fb2\u0f71",
    "ཀ\u0fb2\u0f71\u0f80",
    "一",
    "𠀀",
]
# The third: U+0E01 U+0E4D U+0E32, whose last two code points collate as one and tie it with U+0E01
# U+0E33, beside Thai names that hold one of its parts or neither.
_MADE_3 = [
    "\u0e01\u0e4d\u0e32",
    "\u0e01\u0e33",
    "\u0e01\u0e32",
    "\u0e01\u0e21",
    "\u0e01\u0e4d",
    "\u0e01",
]
_BY_NAME_NATURAL_3 = [
    "\u0e01",
    "\u0e01\u0e4d",
    "\u0e01\u0e21",
    "\u0e01\u0e32",
    "\u0e01\u0e33",
    "\u0e01\u0e4d\u0e32",
]
_BY_NAME_NATURAL_DESC_3 = [
    "\u0e01\u0e33",
    "\u0e01\u0e4d\u0e32",
    "\u0e01\u0e32",
    "\u0e01\u0e21",
    "\u0e01\u0e4d",
    "\u0e01",
]
# The fourth: `ab` with a code point ICU weighs as nothing between its letters, which real tied
# with `ab`.
_MADE_4 = ["ab", "a\u00adb", "aa", "a\u200bb", "ac", "a\ufeffb"]
_BY_NAME_NATURAL_4 = ["aa", "a\ufeffb", "a\u200bb", "a\u00adb", "ab", "ac"]
_BY_NAME_NATURAL_DESC_4 = ["ac", "a\ufeffb", "a\u200bb", "a\u00adb", "ab", "aa"]
# The fifth: a number of 255 digits beside one of 254, digits beside characters ICU weighs as digit
# zero, a Cyrillic `и` whose breve follows its dot below and so collates as `й`, and Tibetan vowel
# signs a sign further on completes; `name_natural desc` was the exact reverse.
_MADE_5 = ["1" + "0" * 254, "2" + "0" * 253, "x5", "x\u2070", "x1", "x\u24ea", "x0"]
_MADE_5 += [
    "\u0438\u0430",
    "\u0438\u0432",
    "\u0439\u0430",
    "\u0439\u0432",
    "\u0438\u0323\u0306\u0431",
]
_MADE_5 += ["\u0f71\u0f72\u0fb3", "\u0f71\u0f71\u0f72", "\u0f71\u0f74\u0fb3", "\u0f71\u0f71\u0f74"]
_BY_NAME_NATURAL_5 = [_MADE_5[i] for i in (0, 1, 6, 4, 2, 5, 3, 7, 8, 9, 11, 10, 12, 13, 14, 15)]
# Replayed without the two names whose vowel sign ICU joins to the first AA sign past the second,
# which `backlot.collation` does not; real's order of the others is the order it listed them in.
_KEPT_5 = [n for n in _MADE_5 if n not in ("\u0f71\u0f71\u0f72", "\u0f71\u0f71\u0f74")]
# The sixth: names that differ in case or in a character's variant, in pairs made in turn; real
# tied an ASCII letter's capital with it and no other.
_MADE_6 = [
    "xa",
    "xA",
    "yA",
    "ya",
    "q\u24ea",
    "q\u2070",
    "z\uff58",
    "zx",
    "\ufb01g",
    "fig",
    "k\u00aa",
    "ka",
    "\u00e4b",
    "a\u0308b",
    "\u00c4c",
    "\u00e4c",
    "w\u00e9",
    "w\u00c9",
    "we",
]
_BY_NAME_NATURAL_6 = [
    _MADE_6[i] for i in (13, 12, 15, 14, 9, 8, 11, 10, 4, 5, 18, 16, 17, 1, 0, 3, 2, 7, 6)
]
_BY_NAME_NATURAL_DESC_6 = [
    _MADE_6[i] for i in (6, 7, 3, 2, 1, 0, 17, 16, 18, 5, 4, 10, 11, 8, 9, 14, 15, 13, 12)
]
# The seventh: an `и` whose breve a mark of its own class keeps from the letter, which real sorted
# among the names with `и`, and Tibetan vowel signs that one sign or two spell alike.
_MADE_7 = ["\u0438\u0430", "\u0439\u0430", "\u0438\u0323\u0301\u0306\u0431", "\u0438\u0432"]
_MADE_7 += [
    "\u0439\u0432",
    "\u0f40\u0f71\u0f74\u0f72",
    "\u0f40\u0f71\u0f72",
    "\u0f40\u0f71\u0f74",
    "\u0f40\u0f73",
]
_BY_NAME_NATURAL_7 = [_MADE_7[i] for i in (0, 2, 3, 1, 4, 8, 6, 7, 5)]
_BY_NAME_NATURAL_DESC_7 = [_MADE_7[i] for i in (5, 7, 8, 6, 4, 1, 3, 2, 0)]
# Replayed without the name whose marks are out of their canonical order, which ICU weighs as
# written and `backlot.collation` after NFC has reordered them.
_KEPT_7 = [n for n in _MADE_7 if n != "\u0f40\u0f71\u0f74\u0f72"]
# The eighth: Tibetan vowel signs whose later sign completes the first one past another, which
# real tied with the same signs written the other way round, listed newest first both ways.
_MADE_8 = ["\u0f71\u0f71\u0f72", "\u0f71\u0f72\u0f71", "\u0f71\u0f71\u0f74", "\u0f71\u0f74\u0f71"]
_MADE_8 += ["\u0f71\u0f71\u0f80", "\u0f71\u0f80\u0f71"]
_BY_NAME_NATURAL_8 = [_MADE_8[i] for i in (1, 0, 5, 4, 3, 2)]
_BY_NAME_NATURAL_DESC_8 = [_MADE_8[i] for i in (3, 2, 5, 4, 1, 0)]
# Eleven names real listed under `name_natural` on 2026-10-10, in a folder made for them in a
# Korean-language account; `name_natural desc` was the exact reverse. None holds Hangul or Han,
# which a Korean-language account orders otherwise than an English-language one.
_ELEVEN = ["2²3", "10²", "²", "a", "x2", "x2①", "x2²", "x3", "x10", "x22", "x²"]
# The ninth folder `backlot.collation` describes: names whose order turns on the entries of its
# tables, made in code point order.
# fmt: off
_MADE_9 = [f"a{c}b" for c in (
    "\t\n\x0b\x0c\r !\"#$%&'()*+,-./:;<=>?@[\\]^_`{|}~\x85\xa1\xa2\xa3\xa4\xa5\xa6\xa7"
    "\xa8\xa9\xab\xac\xae\xaf\xb0\xb1\xb4\xb6\xb7\xb8\xbb\xbf\xd7\xf7\u0e46\u0ec6\u2010"
    "\u2012\u2013\u2014\u2015\u2016\u2017\u2018\u2019\u201a\u201b\u201c\u201d\u201e\u201f"
    "\u2020\u2021\u2022\u2023\u2027\u2028\u2029\u2038\u2044\u20ac\u20ad\u2190\u2191\u2192"
    "\u2193\u2194\u2195\u2196\u2197\u2198\u2199\u219c\u219d\u219e\u219f\u3001\u3002\u3003"
    "\u3005\u3008\u3009\u300a\u300b\u300c\u300d\u300e\u300f\u3010\u3011\u3014\u3015\u301c"
    "\u301d\u301e\u303b\u309d\u30fb\u30fc\u30fd"
)]
_MADE_9 += ["a" + mark + "b" for mark in (
    "\u034f\u0591\u05bd\u05c4\u05c5\u0610\u06d6\u06df\u06e7\u06e8\u06ea\ufe21\ufe23\ufe28"
    "\ufe2a\ufe2f\U000e0100"
)]
_MADE_9 += ["xa"] + ["xa" + mark for mark in (
    "\u0300\u0301\u0302\u0303\u0304\u0306\u0307\u0308\u030a\u030b\u030c\u0310\u0327\u0328"
    "\u0335\u0338"
)]
_MADE_9 += [f"w{a}{b}" for a, b in zip("acefghijkmoqsuw", "\u0305\u05b0\u05bf\u05bf\u05c1\u05c1\u05c7\u05c7\u064b\u0670\u0e47\u0ec8\u20d0\ufe20\u3099")]
_MADE_9 += [f"w{a}{b}" for a in "acefghijkmoqsuw" for b in ("", "b")]
_MADE_9 += [
    "ab", "la", "lb", "l\xb7a", "l\u0387a", "x a", "x,",
    "x10000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000",
    "x1\u20442",
    "x9999999999999999999999999999999999999999999999999999999999999999999999999999999999999999999999999999999999999999999999999999999999999999999999999999999999999999999999999999999999999999999999999999999999999999999999999999999999999999999999999999999999999",
    "x\xa0a", "x\xb9\u2044\u2082", "x\xbd", "x\u0628", "x\u1d2c", "x\u1d43", "x\u2002a",
    "x\u2090", "x\u24b6", "x\u24d0", "x\u3000a", "x\u3002", "x\ufe10", "x\ufe50", "x\ufe8f",
    "x\ufe90", "x\ufe91", "x\ufe92", "x\uff21", "x\uff41", "x\uff61", "x\U0001d400",
    "x\U0001d41a", "x\U0001f130", "yae", "yaf", "yd", "ye", "yh", "yi", "yj", "yl", "ym", "yn",
    "yo", "yoe", "yof", "yp", "yq", "yr", "yss", "yst", "yt", "yu", "yz", "y\xc6", "y\xd0",
    "y\xd8", "y\xde", "y\xdf", "y\xe6", "y\xf0", "y\xf8", "y\xfe", "y\u0110", "y\u0111",
    "y\u0126", "y\u0127", "y\u0131", "y\u0138", "y\u0141", "y\u0142", "y\u014a", "y\u014b",
    "y\u0152", "y\u0153", "y\u0166", "y\u0167", "y\u1e9e", "\u013fa", "\u0140a",
    "\u0430\u0418\u0306", "\u0430\u0419", "\u0430\u0438\u0306", "\u0430\u0438\u043a",
    "\u0430\u0439", "\u0622\u0628", "\u0623\u0628", "\u0624\u0628", "\u0625\u0628",
    "\u0626\u0628", "\u0627\u0628", "\u0627\u062c", "\u0627\u0653\u0628", "\u0627\u0654\u0628",
    "\u0627\u0655\u0628", "\u0648\u0628", "\u0648\u0654\u0628", "\u064a\u0628",
    "\u064a\u0654\u0628", "\u0e02", "\u0e40\u0e01", "\u0e81\u0eb2", "\u0e81\u0eb3",
    "\u0e81\u0ec0", "\u0e81\u0ecd", "\u0e81\u0ecd\u0eb2", "\u0e82", "\u0ec0\u0e01",
    "\u0ec0\u0e81", "\u0f40\u0e33", "\u0f40\u0e4d\u0e32", "\u0f40\u0eb3", "\u0f40\u0ecd\u0eb2",
    "\u0f40\u0f68", "\u0f40\u0f71", "\u0f40\u0f71\u0f72", "\u0f40\u0f71\u0f74",
    "\u0f40\u0f71\u0f80", "\u0f40\u0f72", "\u0f40\u0f73", "\u0f40\u0f74", "\u0f40\u0f75",
    "\u0f40\u0f76", "\u0f40\u0f77", "\u0f40\u0f78", "\u0f40\u0f79", "\u0f40\u0f7a",
    "\u0f40\u0f7b", "\u0f40\u0f7c", "\u0f40\u0f7d", "\u0f40\u0f80", "\u0f40\u0f81",
    "\u0f40\u0f84", "\u0f40\u0f88", "\u0f40\u0f90", "\u0f40\u0fb2\u0f71\u0f80",
    "\u0f40\u0fb2\u0f80", "\u0f40\u0fb3\u0f71\u0f80", "\u0f40\u0fb3\u0f80", "\u0f40\u0fb8",
    "\u0f41", "\u1981", "\u19b5\u1980", "\u2764", "\u2764\ufe0e", "\u2764\ufe0f", "\u3041",
    "\u3041\u3044", "\u3042", "\u304b\u3042", "\u304b\u30fc", "\u30a1", "\u30a2",
    "\u30ab\u30a2", "\u30ab\u30fc", "\u32d0", "\uff67", "\uff71",
]
_MADE_9.sort()
_BY_NAME_NATURAL_9 = [_MADE_9[int(i)] for i in (
    "353 352 351 0 1 2 3 4 39 96 97 5 32 82 18 76 77 78 79 80 132 137 17 116 22 21 6 40 26 60 "
    "19 117 57 12 83 84 85 86 7 87 88 89 90 133 134 49 59 13 14 28 30 35 37 120 121 122 123 "
    "124 125 126 127 128 129 130 131 81 46 56 27 15 20 29 11 8 10 91 92 93 94 95 118 98 33 55 "
    "31 52 47 58 53 48 51 102 104 103 105 106 107 108 109 110 111 112 113 114 115 16 54 62 61 "
    "23 24 25 50 36 45 38 99 74 75 119 135 136 138 139 43 41 9 42 44 100 101 145 144 143 142 "
    "141 140 73 72 71 70 69 68 67 66 65 64 63 34 146 287 149 148 286 147 150 152 151 153 155 "
    "154 156 158 157 159 161 160 162 164 163 165 167 166 168 170 169 171 173 172 174 176 175 "
    "177 179 178 180 182 181 183 185 184 186 188 187 189 191 190 192 194 193 195 227 223 217 "
    "196 230 229 228 237 198 199 197 218 219 200 236 239 226 235 238 225 222 224 240 221 202 "
    "201 206 203 211 209 208 210 204 207 216 213 214 205 215 212 220 233 234 232 231 241 267 "
    "262 242 243 272 271 268 263 244 245 274 273 246 275 247 248 278 277 249 250 280 279 251 "
    "269 264 252 282 281 253 254 255 276 256 257 266 285 258 259 284 283 260 261 270 265 291 "
    "292 290 289 288 300 293 301 294 304 295 302 296 306 297 298 299 303 305 308 307 312 309 "
    "313 310 316 311 314 315 318 317 320 319 342 321 347 341 322 326 327 323 338 339 325 328 "
    "329 324 344 330 343 331 346 332 345 333 334 335 336 337 340 348 350 349 354 356 359 364 "
    "360 365 363 355 358 362 357 361"
).split()]
_BY_NAME_NATURAL_DESC_9 = [_MADE_9[int(i)] for i in (
    "361 357 362 358 355 363 365 360 364 359 356 354 349 350 348 340 337 336 335 334 345 333 "
    "346 332 343 331 344 330 329 324 328 339 325 338 327 323 326 322 341 347 321 342 320 319 "
    "318 317 315 314 316 311 313 310 309 312 307 308 305 303 299 298 306 297 302 296 304 295 "
    "301 294 300 293 289 288 292 290 291 265 270 261 260 283 284 259 258 285 266 257 256 276 "
    "255 254 253 281 282 252 264 269 251 279 280 250 249 277 278 248 247 275 246 273 274 245 "
    "244 263 268 271 272 243 242 262 267 241 231 232 234 233 220 212 215 205 214 213 216 207 "
    "204 210 208 209 211 203 206 201 202 240 221 224 222 225 238 235 226 239 236 200 219 218 "
    "197 199 198 237 228 229 230 196 217 223 227 195 193 194 192 190 191 189 187 188 186 184 "
    "185 183 181 182 180 178 179 177 175 176 174 172 173 171 169 170 168 166 167 165 163 164 "
    "162 160 161 159 157 158 156 154 155 153 151 152 150 147 286 287 149 148 146 145 144 143 "
    "142 141 140 73 72 71 70 69 68 67 66 65 64 63 34 101 100 44 42 9 41 43 139 138 136 135 119 "
    "75 74 99 38 45 36 50 25 24 23 61 62 54 16 115 114 113 112 111 110 109 108 107 106 105 103 "
    "104 102 51 48 53 58 47 52 31 55 33 98 118 95 94 93 92 91 10 8 11 29 20 15 27 56 46 81 131 "
    "130 129 128 127 126 125 124 123 122 121 120 37 35 30 28 14 13 59 49 134 133 90 89 88 87 7 "
    "86 85 84 83 12 57 117 19 60 26 40 6 21 22 116 17 137 132 80 79 78 77 76 18 82 32 5 97 96 "
    "39 4 3 2 1 0 353 352 351"
).split()]
# fmt: on

# The tenth: a digit with no decomposition, Tibetan subjoined consonants beside their consonants, a
# middle dot ahead of an l, control characters, the Hebrew maqaf and paseq, and `가`, which
# an English-language account lists after the Latin names.
_MADE_10 = [
    "a",
    "a\x01b",
    "a-b",
    "ab",
    "ac",
    "a\x7fb",
    "a\u05beb",
    "a\u05c0b",
    "a\u2010b",
    "l",
    "la",
    "x10",
    "x5",
    "xa",
    "x\u278e",
    "\xb7a",
    "\xb7l",
    "\u0f40\u0f40",
    "\u0f40\u0f41",
    "\u0f40\u0f62",
    "\u0f40\u0f63",
    "\u0f40\u0f90",
    "\u0f40\u0fb2",
    "\uac00",
]
_BY_NAME_NATURAL_10 = [
    _MADE_10[i]
    for i in (15, 16, 0, 2, 8, 6, 7, 5, 3, 1, 4, 9, 10, 12, 11, 14, 13, 17, 21, 18, 19, 22, 20, 23)
]
_BY_NAME_NATURAL_DESC_10 = [
    _MADE_10[i]
    for i in (23, 20, 22, 19, 18, 21, 17, 13, 14, 11, 12, 10, 9, 4, 5, 3, 1, 7, 6, 8, 2, 0, 16, 15)
]
# The eleventh: a Tai Viet vowel written ahead of its consonant, in a folder of its own, since ICU
# orders the Tai Viet script among the others otherwise than `backlot.collation` does.
_MADE_11 = ["\uaa80", "\uaa81", "\uaab5\uaa80"]
_BY_NAME_NATURAL_11 = [_MADE_11[i] for i in (0, 2, 1)]
_BY_NAME_NATURAL_DESC_11 = [_MADE_11[i] for i in (1, 2, 0)]
# The twelfth: small kana of the Katakana Phonetic Extensions and of the Small Kana Extension
# beside the letters they tie, and katakana `ア` beside a longer hiragana name.
_MADE_12 = [
    "\u3042",
    "\u3042\u3044",
    "\u304f",
    "\u3053",
    "\u30a2",
    "\u30a2\u3044",
    "\u30af",
    "\u30b3",
    "\u31f0",
    "\U0001b132",
    "\U0001b155",
]
_BY_NAME_NATURAL_12 = [_MADE_12[i] for i in (0, 4, 1, 5, 2, 8, 6, 9, 3, 10, 7)]
_BY_NAME_NATURAL_DESC_12 = [_MADE_12[i] for i in (7, 10, 3, 9, 6, 8, 2, 5, 1, 4, 0)]
# A Python whose `unicodedata` predates Unicode 15 does not know the Small Kana Extension's two
# letters, which `backlot.collation` then sorts by code point, so the replay leaves those out there.
_KEPT_12 = [n for n in _MADE_12 if "Cn" not in map(unicodedata.category, n)]


@pytest.mark.parametrize(
    "made, order_by, listed",
    [
        (_MADE, "name_natural", _BY_NAME_NATURAL),
        (_MADE, "name_natural desc", _BY_NAME_NATURAL_DESC),
        (_MADE, "name", _BY_NAME),
        (_MADE_2, "name_natural", _BY_NAME_NATURAL_2),
        (_MADE_2, "name_natural desc", _BY_NAME_NATURAL_DESC_2),
        (_MADE_2, "name", _BY_NAME_2),
        (_MADE_3, "name_natural", _BY_NAME_NATURAL_3),
        (_MADE_3, "name_natural desc", _BY_NAME_NATURAL_DESC_3),
        (_MADE_4, "name_natural", _BY_NAME_NATURAL_4),
        (_MADE_4, "name_natural desc", _BY_NAME_NATURAL_DESC_4),
        (_KEPT_5, "name_natural", [n for n in _BY_NAME_NATURAL_5 if n in _KEPT_5]),
        (_KEPT_5, "name_natural desc", [n for n in _BY_NAME_NATURAL_5[::-1] if n in _KEPT_5]),
        (_MADE_6, "name_natural", _BY_NAME_NATURAL_6),
        (_MADE_6, "name_natural desc", _BY_NAME_NATURAL_DESC_6),
        (_KEPT_7, "name_natural", [n for n in _BY_NAME_NATURAL_7 if n in _KEPT_7]),
        (_KEPT_7, "name_natural desc", [n for n in _BY_NAME_NATURAL_DESC_7 if n in _KEPT_7]),
        (_MADE_8, "name_natural", _BY_NAME_NATURAL_8),
        (_MADE_8, "name_natural desc", _BY_NAME_NATURAL_DESC_8),
        (_ELEVEN, "name_natural", _ELEVEN),
        (_ELEVEN, "name_natural desc", _ELEVEN[::-1]),
        (_MADE_9, "name_natural", _BY_NAME_NATURAL_9),
        (_MADE_9, "name_natural desc", _BY_NAME_NATURAL_DESC_9),
        (_MADE_10, "name_natural", _BY_NAME_NATURAL_10),
        (_MADE_10, "name_natural desc", _BY_NAME_NATURAL_DESC_10),
        (_MADE_11, "name_natural", _BY_NAME_NATURAL_11),
        (_MADE_11, "name_natural desc", _BY_NAME_NATURAL_DESC_11),
        (_KEPT_12, "name_natural", [n for n in _BY_NAME_NATURAL_12 if n in _KEPT_12]),
        (_KEPT_12, "name_natural desc", [n for n in _BY_NAME_NATURAL_DESC_12 if n in _KEPT_12]),
    ],
)
def test_drive_order_by_a_name_lists_a_folder_as_real_did(tmp_path, made, order_by, listed):
    """Each file is modified after the one made before it, as the measured folder's were, so
    names a key ties come out newest first, as they did on real."""
    s = tiny_corpus(
        tmp_path,
        [
            {
                "source_type": "google_drive",
                "doc_id": f"name-{i}",
                "folder": "names",
                "title": title,
                "content": "p",
                "created": f"2026-10-20T17:{36 + i // 60}:{i % 60:02d}Z",
                "updated": f"2026-10-20T17:{36 + i // 60}:{i % 60:02d}Z",
            }
            for i, title in enumerate(made)
        ],
    )
    admin = yaml.safe_load(s.tokens_path.read_text(encoding="utf-8"))["admin_token"]
    with client_for(s, reload=True) as c:
        r = c.get(
            "/drive/v3/files",
            headers={"Authorization": f"Bearer {admin}"},
            params={
                "orderBy": order_by,
                "q": "mimeType != 'application/vnd.google-apps.folder'",
                "pageSize": 1000,
            },
        )
    assert r.status_code == 200, r.text
    assert [f["name"] for f in r.json()["files"]] == listed


_DUPLICATE_403 = (
    "orderByContainsDuplicateSortKeys",
    "The orderBy parameter cannot contain duplicate sort keys.",
)
_FULLTEXT_403 = (
    "forbidden",
    "Sorting is not supported for queries with fullText terms. Results are always in descending relevance order.",
)
_INVALID_400 = ("invalid", "Invalid Value")
# The eleven keys the reference lists, each a 200 with ` asc` and with ` desc` on real.
_ORDER_KEYS = ["createdTime", "folder", "modifiedByMeTime", "modifiedTime", "name", "name_natural"]
_ORDER_KEYS += ["quotaBytesUsed", "recency", "sharedWithMeTime", "starred", "viewedByMeTime"]
# The code points sent in the three places `_DRIVE_ORDER_SPACE` describes, as real answered them:
# its 25 a 200 in each place, and every other one sent the 400, U+001C-U+001F among them.
_ORDER_SPACES = [*range(0x09, 0x0E), 0x20, 0x85, 0xA0, 0x1680, *range(0x2000, 0x200B)]
_ORDER_SPACES += [0x2028, 0x2029, 0x202F, 0x205F, 0x3000]
_ORDER_NOT_SPACES = [*range(0x00, 0x09), *range(0x0E, 0x20), 0x7F, 0x180E, 0x200B, 0x2060]
_ORDER_NOT_SPACES += [0x3164, 0xFEFF]
_ORDER_SPACE_ROWS = [
    (None, spelling.format(c=chr(cp)), *answer)
    for cps, answer in ((_ORDER_SPACES, (200, None)), (_ORDER_NOT_SPACES, (400, _INVALID_400)))
    for cp in cps
    for spelling in ("name{c}desc", "{c}name", "name{c}")
]


@pytest.mark.parametrize(
    "q, order_by, status, error",
    [
        (None, "name", 200, None),
        (None, "name,modifiedTime", 200, None),
        (None, "name desc,modifiedTime", 200, None),
        (None, "recency,modifiedTime", 200, None),
        (None, "name,name", 403, _DUPLICATE_403),
        (None, "name desc,name", 403, _DUPLICATE_403),
        (None, "name,name desc", 403, _DUPLICATE_403),
        (None, "name desc,name desc", 403, _DUPLICATE_403),
        (None, "modifiedTime,name,modifiedTime", 403, _DUPLICATE_403),
        (None, "name_natural,name", 403, _DUPLICATE_403),
        (None, "name,name_natural", 403, _DUPLICATE_403),
        (None, "name,name,bogus", 403, _DUPLICATE_403),
        (None, "name,bogus,name", 400, _INVALID_400),
        (None, "name,name sideways", 400, _INVALID_400),
        (None, "bogus", 400, _INVALID_400),
        (None, "name descending", 400, _INVALID_400),
        (None, "NAME", 400, _INVALID_400),
        (None, "name ASC", 400, _INVALID_400),
        (None, "name Desc", 400, _INVALID_400),
        (None, "name desc desc", 400, _INVALID_400),
        (None, "name asc desc", 400, _INVALID_400),
        (None, "asc", 400, _INVALID_400),
        (None, "name,asc", 400, _INVALID_400),
        (None, "name asc", 200, None),
        (None, "name   asc", 200, None),
        (None, "name asc,modifiedTime desc", 200, None),
        (None, "folder,name desc", 200, None),
        (None, ",name", 200, None),
        (None, "name,", 200, None),
        (None, "name asc,name", 403, _DUPLICATE_403),
        (None, "name,name asc", 403, _DUPLICATE_403),
        (None, "viewedByMeTime", 200, None),
        (None, "viewedByMeTime desc", 200, None),
        (None, "modifiedByMeTime asc", 200, None),
        (None, "viewedByMeTime,modifiedByMeTime", 200, None),
        (None, "viewedByMeTime,viewedByMeTime desc", 403, _DUPLICATE_403),
        *_ORDER_SPACE_ROWS,
        *[(None, f"{key} {way}", 200, None) for key in _ORDER_KEYS for way in ("asc", "desc")],
        (None, "starred", 200, None),
        (None, "starred desc", 200, None),
        (None, "starred,name", 200, None),
        (None, "starred desc,name", 200, None),
        (None, "starred,name,folder", 200, None),
        (None, ",starred", 200, None),
        (None, "name,starred", 500, None),
        (None, "name, starred", 500, None),
        (None, "name desc,starred desc", 500, None),
        (None, "folder,starred,name", 500, None),
        (None, "name,folder,starred", 500, None),
        (None, "createdTime,starred", 500, None),
        (None, "quotaBytesUsed,starred", 500, None),
        (None, "name,,starred", 500, None),
        (None, "name,starred,name", 403, _DUPLICATE_403),
        (None, "starred,starred", 403, _DUPLICATE_403),
        (None, "name,starred,bogus", 400, _INVALID_400),
        (None, "name asc,starred", 500, None),
        ("fullText contains 'the'", "name", 403, _FULLTEXT_403),
        ("fullText contains 'the'", "name desc", 403, _FULLTEXT_403),
        ("fullText contains 'the'", "name,modifiedTime", 403, _FULLTEXT_403),
        ("fullText contains 'the'", "name,starred", 403, _FULLTEXT_403),
        ("fullText contains 'the'", "name,viewedByMeTime", 403, _FULLTEXT_403),
        ("fullText contains 'the'", "viewedByMeTime", 403, _FULLTEXT_403),
        ("fullText contains 'the'", "viewedByMeTime desc", 403, _FULLTEXT_403),
        ("fullText contains 'the'", "modifiedByMeTime", 403, _FULLTEXT_403),
        ("fullText contains 'the'", "name,name", 403, _DUPLICATE_403),
        ("fullText contains 'the'", "viewedByMeTime,viewedByMeTime", 403, _DUPLICATE_403),
        ("fullText contains 'zzqqxx'", "name", 403, _FULLTEXT_403),
        ("fullText contains 'zzqqxx'", None, 200, None),
        ("fullText contains 'zzqqxx'", "", 200, None),
        ("fullText contains 'zzqqxx' and trashed = false", "name", 403, _FULLTEXT_403),
        ("fullText contains 'zzqqxx' or name contains 'zzqqxx'", "name", 403, _FULLTEXT_403),
        ("not fullText contains 'the'", "name", 403, _FULLTEXT_403),
        ("(fullText contains 'the')", "name", 403, _FULLTEXT_403),
        ("name contains 'the'", "name", 200, None),
        ("name contains 'fullText'", "name", 200, None),
    ],
)
def test_drive_order_by_is_refused_where_real_refuses_it(
    client, admin_h, q, order_by, status, error
):
    """`orderBy` on its own and beside a `q`, answered with the 400 and the 403s
    `_drive_order_specs` and `gerr.sorting_not_supported_fulltext` describe and the 500
    `gerr.drive_internal_error` describes, each `error` object the one real sends. An unusable
    token at or before a repeat is the 400, the parse is refused ahead of the 500 wherever
    `starred` sits and the repeat ahead of the `fullText` 403, and an absent or empty `orderBy`, or
    a `q` with no `fullText` term, is served."""
    params = {"pageSize": 1, "q": q, "orderBy": order_by}
    r = client.get(
        "/drive/v3/files",
        headers=admin_h,
        params={k: v for k, v in params.items() if v is not None},
    )
    assert r.status_code == status, r.text
    if error is not None:
        reason, message = error
        assert _gerr(r) == {
            "code": status,
            "message": message,
            "errors": [
                {
                    "message": message,
                    "domain": "global",
                    "reason": reason,
                    "location": "orderBy",
                    "locationType": "parameter",
                }
            ],
        }
    if status == 500:
        assert _gerr(r) == {
            "code": 500,
            "message": "Internal Error",
            "errors": [
                {"message": "Internal Error", "domain": "global", "reason": "internalError"}
            ],
        }


def test_drive_invalid_fields_mask_is_rejected(client, admin_h):
    """Accepting an unknown field name and yielding empty file objects (200 {}) lets a typo or a
    stale field name in a consumer's mask pass every Backlot-backed test and 400 in production."""
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
    # a documented field Backlot does not synthesize is still valid (real Drive omits it, 200)
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
    """Folders flow through `_drive_q_eval`, so every clause that should match one does."""
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
    """Google populates `size` for binary content *and for Docs Editors files*; Backlot omitted
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
# and the one Backlot had no route for at all (404). Its contract is unusual: `fields` is
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


def test_drive_about_needs_auth(client):
    # no header at all -> 401 (`about` takes no API key, `_NO_CREDENTIAL_ROWS`); a bad token -> 401
    assert client.get(ABOUT, params={"fields": "user"}).status_code == 401
    bad = {"Authorization": "Bearer nope"}
    assert client.get(ABOUT, params={"fields": "user"}, headers=bad).status_code == 401
    # auth is resolved before the mask, as real Drive does — a missing mask on a bad token is 401
    assert client.get(ABOUT, headers=bad).status_code == 401


def test_drive_about_nested_mask_selects_its_parent(client, admin_h):
    """`storageQuota/limit` selects `storageQuota` whole, as `owners/kind` selects `owners` on
    `files.get`."""
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
    assert quota["usage"] == quota["usageInDrive"]  # Backlot stores nothing outside Drive
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
    # every native type Backlot serves, each with real's list in real's order, which is what
    # `_DRIVE_EXPORT_FORMATS` records; the folder type is not exportable anywhere
    assert formats == {
        DOC_MIME: [
            "application/rtf",
            "application/vnd.oasis.opendocument.text",
            "text/html",
            "application/pdf",
            "text/x-markdown",
            "text/markdown",
            "application/epub+zip",
            "application/zip",
            "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            "text/plain",
        ],
        SHEET_MIME: [
            "application/x-vnd.oasis.opendocument.spreadsheet",
            "text/tab-separated-values",
            "application/pdf",
            "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            "text/csv",
            "application/zip",
            "application/vnd.oasis.opendocument.spreadsheet",
        ],
        "application/vnd.google-apps.presentation": [
            "application/vnd.oasis.opendocument.presentation",
            "application/pdf",
            "application/vnd.openxmlformats-officedocument.presentationml.presentation",
            "text/plain",
        ],
    }


def test_drive_about_shared_drive_fields_agree_with_the_drives_listing(client, admin_h):
    """Backlot's corpus is all My Drive and `/drive/v3/drives` is empty, so every shared-drive
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
    # The system parameters beside the route's own, declared on every family operation at once
    # (see test_every_google_operation_declares_the_system_parameters_and_reads_them).
    assert {p["name"] for p in op["parameters"]} == {
        "fields",
        "$.xgafv",
        "access_token",
        "oauth_token",
    }


@pytest.fixture(scope="module")
def base(live_server):
    return live_server[0]


@pytest.fixture(scope="module")
def admin_h(live_server):
    return {"Authorization": f"Bearer {live_server[1].admin_token}"}


def _drive_by_mime(base, admin_h, mime, *, name: str | None = None):
    """A visible Drive file id + name for the given native mimeType.

    ``name`` picks a SPECIFIC file, and callers that go on to assert the file's contents pass it.
    Taking "whichever comes first" made those assertions depend on the listing order, which is the
    order of an opaque synthesized id — so a corpus holding two spreadsheets could hand a
    content assertion the wrong one, and the test would read as a content bug rather than as the
    arbitrary pick it was."""
    r = httpx.get(
        f"{base}/drive/v3/files", headers=admin_h, params={"q": "trashed=false", "pageSize": 1000}
    ).json()
    for f in r["files"]:
        if f["mimeType"] == mime and (name is None or f["name"] == name):
            return f["id"], f["name"]
    raise AssertionError(f"no {mime} named {name!r} in corpus" if name else f"no {mime} in corpus")


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


@pytest.mark.parametrize(
    "params",
    [{}, {"suggestionsViewMode": "PREVIEW_WITHOUT_SUGGESTIONS"}, {"includeTabsContent": "true"}],
    ids=["plain", "preview-without-suggestions", "tabs-content"],
)
def test_docs_get_serves_members_in_reals_order(base, admin_h, params):
    """A document read serves its members in real's order, `commentsViewMode` last with the value
    real serves. The tabbed shape `includeTabsContent=true` selects is not served, so that row
    checks the last member alone.
    """
    fid, _ = _drive_by_mime(base, admin_h, "application/vnd.google-apps.document")
    r = httpx.get(f"{base}/docs/v1/documents/{fid}", headers=admin_h, params=params)
    assert r.status_code == 200
    assert list(r.json().items())[-1] == ("commentsViewMode", "COMMENTS_VIEW_MODE_OMITTED")
    if "includeTabsContent" not in params:
        assert list(r.json()) == [
            "title",
            "body",
            "documentStyle",
            "namedStyles",
            "revisionId",
            "suggestionsViewMode",
            "documentId",
            "commentsViewMode",
        ]


_DOCS_SUGGESTIONS = "type.googleapis.com/google.apps.docs.v1.SuggestionsViewMode"
_DOCS_COMMENTS = "type.googleapis.com/google.apps.docs.v1.CommentsViewMode"


def _docs_invalid(field, typ, value):
    message = f"Invalid value at '{field}' ({typ}), \"{value}\""
    return {
        "code": 400,
        "message": message,
        "status": "INVALID_ARGUMENT",
        "details": [
            {
                "@type": "type.googleapis.com/google.rpc.BadRequest",
                "fieldViolations": [{"field": field, "description": message}],
            }
        ],
    }


def _docs_query_rows():
    """Each spelling `docs_get` records, as ``(query pairs, refusal or None)``."""
    suggestions = (
        "DEFAULT_FOR_CURRENT_ACCESS",
        "SUGGESTIONS_INLINE",
        "PREVIEW_SUGGESTIONS_ACCEPTED",
        "PREVIEW_WITHOUT_SUGGESTIONS",
        "suggestions_inline",
        "Suggestions_Inline",
        "0",
        "1",
        "2",
        "3",
        "+1",
        "01",
    )
    comments = (
        "COMMENTS_VIEW_MODE_UNSPECIFIED",
        "COMMENTS_VIEW_MODE_DEFAULT_FOR_CURRENT_ACCESS",
        "COMMENTS_VIEW_MODE_OMITTED",
        "COMMENTS_VIEW_MODE_INCLUDED",
    )
    rows = [([("suggestionsViewMode", name)], None) for name in suggestions]
    rows += [
        (
            [("suggestionsViewMode", value)],
            _docs_invalid("suggestions_view_mode", _DOCS_SUGGESTIONS, value),
        )
        for value in ("4", "-1", "1.0", " 1", "", "NOPE")
    ]
    rows += [
        ([("includeTabsContent", value)], None)
        for value in ("true", "TRUE", "True", "1", "t", "yes", "false", "0", "f", "no", "n", "y")
    ]
    rows += [
        (
            [("includeTabsContent", value)],
            _docs_invalid("include_tabs_content", "TYPE_BOOL", value),
        )
        for value in ("", "NOPE", " true", "on", "off", "2")
    ]
    # Real reads a comments view only beside the tabs content, and `COMMENTS_VIEW_MODE_INCLUDED`
    # only beside inline suggestions; without them it refuses the combination after the lookup.
    tabs = [("includeTabsContent", "true")]
    rows += [([("commentsViewMode", name), *tabs], None) for name in comments[:3]]
    rows += [
        ([("commentsViewMode", value), *tabs], None)
        for value in ("2", "+1", "comments_view_mode_omitted")
    ]
    rows += [
        (
            [
                ("commentsViewMode", comments[3]),
                *tabs,
                ("suggestionsViewMode", "SUGGESTIONS_INLINE"),
            ],
            None,
        )
    ]
    rows += [
        (
            [("commentsViewMode", value)],
            _docs_invalid("comments_view_mode", _DOCS_COMMENTS, value),
        )
        for value in ("", "NOPE", "4", "-1")
    ]
    return rows


_DOCS_GET_VALUES = _docs_query_rows() + [
    (
        [("suggestionsViewMode", "NOPE"), ("suggestionsViewMode", "SUGGESTIONS_INLINE")],
        _docs_invalid("suggestions_view_mode", _DOCS_SUGGESTIONS, "NOPE"),
    ),
    (
        [("suggestionsViewMode", "SUGGESTIONS_INLINE"), ("suggestionsViewMode", "NOPE")],
        _docs_invalid("suggestions_view_mode", _DOCS_SUGGESTIONS, "NOPE"),
    ),
    (
        [
            ("suggestionsViewMode", "DEFAULT_FOR_CURRENT_ACCESS"),
            ("suggestionsViewMode", "PREVIEW_WITHOUT_SUGGESTIONS"),
        ],
        None,
    ),
    (
        [("includeTabsContent", "NOPE"), ("includeTabsContent", "true")],
        _docs_invalid("include_tabs_content", "TYPE_BOOL", "NOPE"),
    ),
    (
        [("includeTabsContent", "false"), ("includeTabsContent", "NOPE")],
        _docs_invalid("include_tabs_content", "TYPE_BOOL", "NOPE"),
    ),
    ([("includeTabsContent", "true"), ("includeTabsContent", "false")], None),
]


@pytest.fixture(scope="module")
def document_id(client, tokens_yaml):
    headers = {"Authorization": f"Bearer {tokens_yaml['admin_token']}"}
    files = client.get("/drive/v3/files", headers=headers, params={"pageSize": 1000}).json()[
        "files"
    ]
    return next(f["id"] for f in files if f["mimeType"] == "application/vnd.google-apps.document")


@pytest.mark.parametrize("query, refusal", _DOCS_GET_VALUES)
def test_documents_get_reads_a_query_value_real_reads_and_refuses_one_it_cannot(
    client, tokens_yaml, document_id, query, refusal
):
    """The spellings `docs_get` records. A repeat holding a refused value is that value's 400
    whichever end it is at; two values that are read are served. At `$.xgafv=1` the refusal's
    `errors` entry is the one `backlot.errors.google.invalid_field_value` records."""
    headers = {"Authorization": f"Bearer {tokens_yaml['admin_token']}"}
    r = client.get(f"/docs/v1/documents/{document_id}?{urlencode(query)}", headers=headers)
    if refusal is None:
        assert r.status_code == 200, r.text
        assert r.json()["documentId"] == document_id
        return
    assert r.status_code == 400, r.text
    assert r.json()["error"] == refusal
    verbose = client.get(
        f"/docs/v1/documents/{document_id}?{urlencode([*query, ('$.xgafv', '1')])}",
        headers=headers,
    )
    assert verbose.json()["error"] == {
        **refusal,
        "errors": [{"message": refusal["message"], "reason": "invalid"}],
    }


@pytest.mark.parametrize(
    "query, who, target, status, needle",
    [
        ("suggestionsViewMode=NOPE", "none", "doc", 401, "UNAUTHENTICATED"),
        ("suggestionsViewMode=NOPE", "bad", "doc", 401, "UNAUTHENTICATED"),
        ("suggestionsViewMode=NOPE&$.xgafv=3", "none", "doc", 400, "$.xgafv"),
        ("suggestionsViewMode=NOPE&$.xgafv=3", "bad", "doc", 400, "$.xgafv"),
        ("suggestionsViewMode=NOPE&$.xgafv=3", "admin", "doc", 400, "$.xgafv"),
        ("suggestionsViewMode=NOPE", "admin", "missing", 400, "suggestions_view_mode"),
        ("includeTabsContent=NOPE", "admin", "missing", 400, "include_tabs_content"),
        ("commentsViewMode=NOPE", "admin", "missing", 400, "comments_view_mode"),
    ],
)
def test_documents_get_answers_a_bad_query_after_the_credential_and_before_the_lookup(
    client, tokens_yaml, document_id, query, who, target, status, needle
):
    """A bad token or none is the 401, and `$.xgafv=3` is that parameter's 400, ahead of a value
    `docs_get` cannot read. On an id that does not exist the same value is the 400, where the
    request without it is the 404."""
    headers = {
        "none": {},
        "bad": BAD_TOKEN,
        "admin": {"Authorization": f"Bearer {tokens_yaml['admin_token']}"},
    }[who]
    document = document_id if target == "doc" else "nosuchfile000"
    r = client.get(f"/docs/v1/documents/{document}?{query}", headers=headers)
    assert r.status_code == status, r.text
    assert needle in r.text
    if needle != "suggestions_view_mode":
        assert "suggestions_view_mode" not in r.text
    if status == 401:
        assert r.content == client.get(f"/docs/v1/documents/{document}", headers=headers).content
    if target == "missing":
        assert client.get(f"/docs/v1/documents/{document}", headers=headers).status_code == 404


def test_sheets_get_withholds_grid_data_by_default(base, admin_h):
    """Measured: a plain `spreadsheets.get` returns `sheets[i].properties` and NO `data` — on a real
    workbook that is 4 KB against 5.7 MB with `includeGridData=true`. Volunteering the full grid on
    every call hands a reader cells it would never get from Google, so the document it builds has a
    different layout in the two environments.

    `ranges` alone does not unlock it either — also measured."""
    fid, _ = _drive_by_mime(
        base, admin_h, "application/vnd.google-apps.spreadsheet", name="Q1 Revenue Model"
    )
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
    """One row per stored line, one cell per row holding the line verbatim. Splitting on commas
    manufactures columns out of prose punctuation over the real corpus — see `_sheets_grid`; the
    corpus has no delimiter-uniform CSV at all."""
    fid, _ = _drive_by_mime(
        base, admin_h, "application/vnd.google-apps.spreadsheet", name="Q1 Revenue Model"
    )
    sh = httpx.get(
        f"{base}/sheets/v4/spreadsheets/{fid}", headers=admin_h, params={"includeGridData": "true"}
    ).json()
    # measured: real Sheets always carries these two beside title and locale
    assert sh["properties"]["autoRecalc"] == "ON_CHANGE"
    assert sh["properties"]["timeZone"] == "Etc/GMT"
    data = sh["sheets"][0]["data"][0]
    assert "startRow" not in data and "startColumn" not in data, "zeros are omitted, as proto3 does"
    # one metadata entry per row and column of the RANGE, which unscoped is the whole grid, each
    # carrying the default track size — measured
    assert data["rowMetadata"] == [{"pixelSize": 21}] * 1000
    assert data["columnMetadata"] == [{"pixelSize": 100}] * 26
    rows = data["rowData"]
    # the values end at the row's last cell holding a value — real shape
    assert {len(r["values"]) for r in rows} == {1}
    assert [r["values"][0]["formattedValue"] for r in rows] == [
        "month,revenue",
        "Jan,120000",
        "Feb,135000",
    ]


def test_sheets_get_grid_data_honours_ranges(base, admin_h):
    """Measured: `ranges` + `includeGridData` scopes the returned rowData to the range (a real
    workbook went 5.7 MB -> 11 KB for `A1:B2`)."""
    fid, _ = _drive_by_mime(
        base, admin_h, "application/vnd.google-apps.spreadsheet", name="Q1 Revenue Model"
    )
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
    Serving these 200 reinterprets the file: a Doc read through the Sheets API comes back as a
    "grid" of prose, plausible enough that a client would trust it."""
    doc, _ = _drive_by_mime(base, admin_h, "application/vnd.google-apps.document")
    sheet, _ = _drive_by_mime(
        base, admin_h, "application/vnd.google-apps.spreadsheet", name="Q1 Revenue Model"
    )
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


def test_editor_apis_distinguish_hidden_type_permission_and_missing(tmp_path):
    """Each editor API's answers for the files a scoped token cannot see, as `_editor_doc` reads
    them, beside the files it can see. At `$.xgafv=1` the 403's entry is ``forbidden`` and the
    Office 400's ``failedPrecondition``."""
    hidden = ("spreadsheet", "document", "presentation", "pdf", "xlsx", "docx", "pptx")
    records = [
        {
            "source_type": "google_drive",
            "doc_id": f"hidden-{kind}",
            "title": kind,
            "content": "private",
            "subtype": kind,
            "author_email": "owner@acme.com",
            "readers": ["owner@acme.com"],
        }
        for kind in hidden
    ]
    records += [
        {
            "source_type": "google_drive",
            "doc_id": f"mia-{kind}",
            "title": kind,
            "content": "public",
            "subtype": kind,
            "author_email": "mia@acme.com",
            "visibility": "public",
        }
        for kind in ("spreadsheet", "document", "presentation")
    ]
    settings = tiny_corpus(tmp_path, records)
    token_data = yaml.safe_load(settings.tokens_path.read_text(encoding="utf-8"))
    tokens = {u["email"]: u["token"] for u in token_data["users"]}
    outsider = {"Authorization": f"Bearer {tokens['mia@acme.com']}"}
    sheet, doc, deck, pdf, xlsx, docx, pptx = (
        served_id("google_drive", f"hidden-{k}") for k in hidden
    )
    own_sheet = served_id("google_drive", "mia-spreadsheet")
    own_doc = served_id("google_drive", "mia-document")
    own_deck = served_id("google_drive", "mia-presentation")

    def refusal(code, message, status):
        return {"error": {"code": code, "message": message, "status": status}}

    permission = refusal(403, "The caller does not have permission", "PERMISSION_DENIED")
    not_found = refusal(404, NOT_FOUND, "NOT_FOUND")
    invalid = refusal(400, INVALID_ARG, "INVALID_ARGUMENT")
    office = refusal(400, OFFICE_MSG, "FAILED_PRECONDITION")

    rows = [
        (f"/sheets/v4/spreadsheets/{own_sheet}", 200, None),
        (f"/docs/v1/documents/{own_doc}", 200, None),
        (f"/slides/v1/presentations/{own_deck}", 200, None),
        (f"/sheets/v4/spreadsheets/{sheet}", 403, permission),
        (f"/docs/v1/documents/{doc}", 403, permission),
        (f"/slides/v1/presentations/{deck}", 403, permission),
        (
            f"/docs/v1/documents/{doc}?suggestionsViewMode=NOPE",
            400,
            {"error": _docs_invalid("suggestions_view_mode", _DOCS_SUGGESTIONS, "NOPE")},
        ),
        (f"/docs/v1/documents/{own_doc}?suggestionsViewMode=SUGGESTIONS_INLINE", 200, None),
        (f"/sheets/v4/spreadsheets/{xlsx}", 400, office),
        (f"/docs/v1/documents/{docx}", 400, office),
        (f"/slides/v1/presentations/{pptx}", 400, office),
        (f"/sheets/v4/spreadsheets/{pdf}", 400, invalid),
        (f"/docs/v1/documents/{pdf}", 400, invalid),
        (f"/slides/v1/presentations/{pdf}", 400, invalid),
        (f"/sheets/v4/spreadsheets/{docx}", 400, invalid),
        (f"/docs/v1/documents/{xlsx}", 400, invalid),
        (f"/slides/v1/presentations/{xlsx}", 400, invalid),
        (f"/slides/v1/presentations/{docx}", 400, invalid),
        (f"/sheets/v4/spreadsheets/{doc}", 404, not_found),
        (f"/docs/v1/documents/{sheet}", 404, not_found),
        (f"/slides/v1/presentations/{doc}", 404, not_found),
        (f"/slides/v1/presentations/{sheet}", 404, not_found),
        ("/sheets/v4/spreadsheets/nosuchfile000", 404, not_found),
        ("/docs/v1/documents/nosuchfile000", 404, not_found),
        ("/slides/v1/presentations/nosuchfile000", 404, not_found),
    ]
    with client_for(settings, reload=True) as client:
        for path, status, body in rows:
            response = client.get(path, headers=outsider)
            assert response.status_code == status, path
            if body is not None:
                assert response.json() == body, path
        for path, message, reason in (
            (f"/sheets/v4/spreadsheets/{sheet}", permission["error"]["message"], "forbidden"),
            (f"/slides/v1/presentations/{deck}", permission["error"]["message"], "forbidden"),
            (f"/docs/v1/documents/{docx}", OFFICE_MSG, "failedPrecondition"),
            (f"/slides/v1/presentations/{pptx}", OFFICE_MSG, "failedPrecondition"),
        ):
            verbose = client.get(path, headers=outsider, params={"$.xgafv": "1"})
            assert verbose.json()["error"]["errors"] == [
                {"message": message, "domain": "global", "reason": reason}
            ], path


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
# corpus says Backlot has no business guessing one: of its 1,875 `doc_type: sheet` records, NONE
# is delimiter-uniform CSV — 82.6% are prose and 17.4% are prose around a PIPE-delimited table.

GRID = [["month,revenue"], ["Jan,120000"], ["Feb,135000"]]


@pytest.fixture(scope="module")
def sheet_id(base, admin_h):
    fid, _ = _drive_by_mime(
        base, admin_h, "application/vnd.google-apps.spreadsheet", name="Q1 Revenue Model"
    )
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
        # R1C1 notation, which the discovery document names beside A1 for `range`
        ("R1C1:R3C1", GRID),
        ("Sheet1!R1C1:R2C2", GRID[:2]),  # column B is empty, so it trims away
        ("'Sheet1'!R2C1", [["Jan,120000"]]),  # one cell, quoted sheet name
        ("r1c1:r3c1", GRID),  # case-insensitive, as the A1 side is
        ("R[0]C[0]:R[2]C[0]", GRID),  # bracketed offsets from A1
        ("R[1]", [["Jan,120000"]]),  # row 2 whole, trimmed to its one cell
        ("RC", [GRID[0]]),  # bare letters are the cell A1
        ("R1C:R3C", GRID),  # a bare C is column A
        ("!A1:A3", GRID),  # an empty title is the first sheet
    ],
)
def test_sheets_values_get_range_forms(base, admin_h, sheet_id, rng, expected):
    """Every form a client may send has to resolve against the same grid. Without the parser each
    of these is a 404 on a route that does not exist; without the R1C1 half, the LlamaIndex
    `GoogleSheetsReader` — which reads every sheet as `R1C1:R{rowCount}C{columnCount}` — 400d on
    its first values call against every spreadsheet."""
    r = _values(base, admin_h, sheet_id, rng)
    assert r.status_code == 200, r.text
    assert r.json()["values"] == expected


def test_sheets_values_get_keeps_a_line_intact(base, admin_h, sheet_id):
    """The cell holds the whole line, commas and all. Splitting on commas manufactures columns out
    of sentence punctuation over the real corpus — a prose line like "customer dates, ARR exposure,
    highest-risk deals" becomes three cells of a table that never existed. Which delimiter (if any)
    applies is the corpus owner's call, not Backlot's."""
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
    from backlot import synth

    # `gd-blankline`'s served id, built to reach a known fixture by its stable
    # doc_id -- unlike an assertion, constructing a REQUEST url this way is the same precedent
    # test_gmail_attachment_size_matches_part_metadata uses (`row["id"]`
    # to build the message url), not the "assert synth.fn(doc_id) at a route" anti-pattern.
    served_id = synth.gdrive_file_id("gd-blankline")
    j = _values(base, admin_h, served_id, "Sheet1").json()
    assert j["values"] == [["header"], [], ["row after gap"]]
    # and the round trip still holds, blank lines and all — trailing gaps included
    export = httpx.get(
        f"{base}/drive/v3/files/{served_id}/export",
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


@pytest.mark.parametrize(
    "rng",
    [
        "Other!A1:B2",
        "not a range",
        "A1:",
        " !A1",  # an empty title is the first sheet; a whitespace one is nothing
        "R1C1:",
        "R0C1",  # absolute R1C1 is 1-based
        "R[-1]C[0]",  # an offset is not negative
        "A1:R2C2",  # both halves of a range are one notation
        "R2C2:R1C1",  # reversed by one, on the numbers as written
        "A0",
        "Z",  # a lone column is no range, so a bang-less one is a sheet name — and there is none
        "Sheet1!B",
        " A1",  # not stripped
    ],
)
def test_sheets_values_get_rejects_an_unusable_range(base, admin_h, sheet_id, rng):
    """Every unusable range is real's `Unable to parse range: <spec>`, whatever made it unusable —
    a sheet this workbook does not have, a malformed reference, an absolute R1C1 index of 0, a
    negative offset, a range that mixes the two notations, a reversal by one on the numbers as
    written, row 0, a lone column (bang-less, read as a sheet name, and there is none), whitespace.
    Each case is one representative of a rule `_R1C1_END`'s comment states; the measured rows
    themselves are `MEASURED_R1C1`, so a new case belongs here when it stands for a rule and there
    when it is a request that was sent."""
    r = _values(base, admin_h, sheet_id, rng)
    assert r.status_code == 400
    assert r.json()["error"]["message"] == f"Unable to parse range: {rng}"


@pytest.mark.parametrize(
    "params, field, enum",
    [
        ({"majorDimension": "DIAGONAL"}, "major_dimension", "Dimension"),
        ({"valueRenderOption": "NOPE"}, "value_render_option", "ValueRenderOption"),
        # Declared in the route's parameter list, so the discovery diff sees it as served; it has
        # to be validated too, or the one value a client could get wrong passes silently.
        ({"dateTimeRenderOption": "NOPE"}, "date_time_render_option", "DateTimeRenderOption"),
        # An EMPTY value is not an absent one: measured, all three 400 on it rather than falling
        # back to the default.
        ({"majorDimension": ""}, "major_dimension", "Dimension"),
        ({"valueRenderOption": ""}, "value_render_option", "ValueRenderOption"),
        ({"dateTimeRenderOption": ""}, "date_time_render_option", "DateTimeRenderOption"),
        # A number the enum does not have, one written as a decimal or behind a space, and the
        # lower-camel name (`protojson.enum_from_string`).
        ({"majorDimension": "3"}, "major_dimension", "Dimension"),
        ({"majorDimension": "-1"}, "major_dimension", "Dimension"),
        ({"majorDimension": "2.0"}, "major_dimension", "Dimension"),
        ({"majorDimension": " 2"}, "major_dimension", "Dimension"),
        ({"majorDimension": "dimensionUnspecified"}, "major_dimension", "Dimension"),
        ({"valueRenderOption": "3"}, "value_render_option", "ValueRenderOption"),
        ({"dateTimeRenderOption": "2"}, "date_time_render_option", "DateTimeRenderOption"),
        # A letter outside ASCII keeps its case (`protojson.enum_from_string`).
        ({"majorDimension": "rowſ"}, "major_dimension", "Dimension"),
        ({"majorDimension": "ROWſ"}, "major_dimension", "Dimension"),
    ],
)
def test_sheets_values_get_rejects_a_bad_enum(base, admin_h, sheet_id, params, field, enum):
    """Measured message shape, not an invented one: Google names the proto field and type, and
    echoes the value as the client spelled it."""
    r = _values(base, admin_h, sheet_id, "Sheet1!A1:A2", **params)
    assert r.status_code == 400
    bad = next(iter(params.values()))
    message = (
        f"Invalid value at '{field}' (type.googleapis.com/google.apps.sheets.v4.{enum}), \"{bad}\""
    )
    assert r.json()["error"]["message"] == message
    assert r.json()["error"]["details"] == _bad_request([(field, message)])


@pytest.mark.parametrize(
    "value, included",
    [
        ("true", True),
        ("TRUE", True),
        ("TrUe", True),
        ("1", True),
        ("t", True),
        ("y", True),
        ("YES", True),
        ("false", False),
        ("0", False),
        ("f", False),
        ("n", False),
        ("NO", False),
    ],
)
def test_include_grid_data_takes_every_boolean_spelling_the_real_api_takes(
    base, admin_h, sheet_id, value, included
):
    """Measured: the flag is parsed as a protobuf boolean, so `1`, `t`, `y` and `yes` mean true and
    `0`, `f`, `n` and `no` mean false, case-insensitively. Matching only the word "true" would
    withhold the grid from a client that asked for it with `includeGridData=1`."""
    r = httpx.get(
        f"{base}/sheets/v4/spreadsheets/{sheet_id}",
        headers=admin_h,
        params={"includeGridData": value},
    )
    assert r.status_code == 200
    assert ("data" in r.json()["sheets"][0]) is included


@pytest.mark.parametrize("param", ["includeGridData", "excludeTablesInBandedRanges"])
@pytest.mark.parametrize(
    "value",
    # `yeſ`: `protojson.to_bool` folds the case of ASCII letters only.
    ["NOPE", "", "2", "01", "1.0", "on", "off", " true", "true ", "yeſ"],
)
def test_a_boolean_query_param_refuses_what_is_not_a_boolean(base, admin_h, sheet_id, param, value):
    """Measured message shape, which names the proto type rather than a message name: `Invalid
    value at 'include_grid_data' (TYPE_BOOL), "NOPE"`. Surrounding whitespace is not trimmed, and
    `on`/`off` are not booleans here."""
    field = {"includeGridData": "include_grid_data"}.get(param, "exclude_tables_in_banded_ranges")
    r = httpx.get(
        f"{base}/sheets/v4/spreadsheets/{sheet_id}", headers=admin_h, params={param: value}
    )
    assert r.status_code == 400
    message = f"Invalid value at '{field}' (TYPE_BOOL), \"{value}\""
    assert r.json()["error"]["message"] == message
    assert r.json()["error"]["details"] == _bad_request([(field, message)])


def _bad_request(violations):
    return [
        {
            "@type": "type.googleapis.com/google.rpc.BadRequest",
            "fieldViolations": [{"field": f, "description": m} for f, m in violations],
        }
    ]


_DIM = "Invalid value at 'major_dimension' (type.googleapis.com/google.apps.sheets.v4.Dimension), "
_RENDER = (
    "Invalid value at 'value_render_option' "
    "(type.googleapis.com/google.apps.sheets.v4.ValueRenderOption), "
)
_DATETIME = (
    "Invalid value at 'date_time_render_option' "
    "(type.googleapis.com/google.apps.sheets.v4.DateTimeRenderOption), "
)
_PAGE_SIZE = "Invalid value at 'page_size' (TYPE_INT32), "
_GRID = "Invalid value at 'include_grid_data' (TYPE_BOOL), "


@pytest.mark.parametrize(
    "method, path, query, body, refused",
    [
        # a bad value at either end of a parameter read last, and both of two bad ones
        (
            "GET",
            "values",
            [("majorDimension", "NOPE"), ("majorDimension", "ROWS")],
            None,
            [("major_dimension", _DIM + '"NOPE"')],
        ),
        (
            "GET",
            "values",
            [("majorDimension", "ROWS"), ("majorDimension", "NOPE")],
            None,
            [("major_dimension", _DIM + '"NOPE"')],
        ),
        (
            "GET",
            "values",
            [("majorDimension", "NOPE1"), ("majorDimension", "NOPE2")],
            None,
            [("major_dimension", _DIM + '"NOPE1"'), ("major_dimension", _DIM + '"NOPE2"')],
        ),
        # several parameters: each one's refusals together and in query order, the parameters in
        # an order real varies from one request to the next
        (
            "GET",
            "values",
            [("valueRenderOption", "NOPE"), ("majorDimension", "NOPE")],
            None,
            [("value_render_option", _RENDER + '"NOPE"'), ("major_dimension", _DIM + '"NOPE"')],
        ),
        (
            "GET",
            "values",
            [
                ("majorDimension", "NOPE1"),
                ("valueRenderOption", "NOPE2"),
                ("majorDimension", "NOPE3"),
            ],
            None,
            [
                ("major_dimension", _DIM + '"NOPE1"'),
                ("value_render_option", _RENDER + '"NOPE2"'),
                ("major_dimension", _DIM + '"NOPE3"'),
            ],
        ),
        (
            "GET",
            "values",
            [
                ("dateTimeRenderOption", "NOPE"),
                ("majorDimension", "NOPE"),
                ("valueRenderOption", "NOPE"),
            ],
            None,
            [
                ("date_time_render_option", _DATETIME + '"NOPE"'),
                ("major_dimension", _DIM + '"NOPE"'),
                ("value_render_option", _RENDER + '"NOPE"'),
            ],
        ),
        # a typed refusal is reached ahead of a mask the response has no field for
        (
            "GET",
            "values",
            [("fields", "bogus"), ("majorDimension", "NOPE")],
            None,
            [("major_dimension", _DIM + '"NOPE"')],
        ),
        (
            "GET",
            "book",
            [("includeGridData", "true"), ("includeGridData", "NOPE")],
            None,
            [("include_grid_data", _GRID + '"NOPE"')],
        ),
        (
            "GET",
            "book",
            [("includeGridData", "NOPE"), ("includeGridData", "true")],
            None,
            [("include_grid_data", _GRID + '"NOPE"')],
        ),
        # Drive's pageSize is read first, and every repeat is still parsed
        (
            "GET",
            "files",
            [("pageSize", "2"), ("pageSize", "NOPE")],
            None,
            [("page_size", _PAGE_SIZE + '"NOPE"')],
        ),
        (
            "GET",
            "files",
            [("pageSize", "NOPE"), ("pageSize", "2")],
            None,
            [("page_size", _PAGE_SIZE + '"NOPE"')],
        ),
        (
            "GET",
            "files",
            [("pageSize", "NOPE1"), ("pageSize", "NOPE2")],
            None,
            [("page_size", _PAGE_SIZE + '"NOPE1"'), ("page_size", _PAGE_SIZE + '"NOPE2"')],
        ),
        (
            "GET",
            "files",
            [("pageSize", "0"), ("pageSize", "NOPE")],
            None,
            [("page_size", _PAGE_SIZE + '"NOPE"')],
        ),
        # Drive's typed booleans join `pageSize`'s refusal
        (
            "GET",
            "files",
            [
                ("pageSize", "NOPE"),
                ("supportsAllDrives", "NOPE"),
                ("includeItemsFromAllDrives", "N2"),
            ],
            None,
            [
                ("page_size", _PAGE_SIZE + '"NOPE"'),
                (
                    "supports_all_drives",
                    "Invalid value at 'supports_all_drives' (TYPE_BOOL), \"NOPE\"",
                ),
                (
                    "include_items_from_all_drives",
                    "Invalid value at 'include_items_from_all_drives' (TYPE_BOOL), \"N2\"",
                ),
            ],
        ),
        # every bad enum in a JSON body, in the body's order
        (
            "POST",
            "values_by_filter",
            [],
            {
                "dataFilters": [{"a1Range": "Sheet1!A1"}],
                "majorDimension": "NOPE",
                "valueRenderOption": "NOPE2",
                "dateTimeRenderOption": "NOPE3",
            },
            [
                ("major_dimension", _DIM + '"NOPE"'),
                ("value_render_option", _RENDER + '"NOPE2"'),
                ("date_time_render_option", _DATETIME + '"NOPE3"'),
            ],
        ),
        (
            "POST",
            "values_by_filter",
            [],
            {
                "dateTimeRenderOption": "NOPE3",
                "majorDimension": "NOPE",
                "dataFilters": [{"a1Range": "Sheet1!A1"}],
                "valueRenderOption": "NOPE2",
            },
            [
                ("date_time_render_option", _DATETIME + '"NOPE3"'),
                ("major_dimension", _DIM + '"NOPE"'),
                ("value_render_option", _RENDER + '"NOPE2"'),
            ],
        ),
        # the same refusal from a JSON body
        (
            "POST",
            "by_filter",
            [],
            {"dataFilters": [{"a1Range": "Sheet1!A1"}], "includeGridData": "NOPE"},
            [("include_grid_data", _GRID + '"NOPE"')],
        ),
    ],
)
def test_every_typed_value_is_parsed_and_every_one_refused_is_named(
    base, admin_h, sheet_id, method, path, query, body, refused
):
    """The rule `_typed_query` and `gerr.invalid_field_values` record, on Sheets and Drive: every
    repeat parsed and every value the proto layer cannot read named in one 400. `refused` is in the
    order sent. A body's refusals are compared exactly, in the order `protojson.read` records; a
    query's keep each field's refusals in that order and leave the order between fields open, as
    `_typed_query` records real does."""
    url = {
        "values": f"/sheets/v4/spreadsheets/{sheet_id}/values/Sheet1!A1",
        "book": f"/sheets/v4/spreadsheets/{sheet_id}",
        "by_filter": f"/sheets/v4/spreadsheets/{sheet_id}:getByDataFilter",
        "values_by_filter": f"/sheets/v4/spreadsheets/{sheet_id}/values:batchGetByDataFilter",
        "files": "/drive/v3/files",
    }[path]
    # The query string is built here rather than handed to httpx as `params`, which groups a
    # repeated key's values together and so would never send an interleaved query.
    target = base + url + ("?" + urlencode(query) if query else "")
    r = httpx.request(method, target, headers=admin_h, json=body)
    assert r.status_code == 400, r.text
    err = r.json()["error"]
    [bad_request] = err["details"]
    assert bad_request["@type"] == "type.googleapis.com/google.rpc.BadRequest"
    got = [(v["field"], v["description"]) for v in bad_request["fieldViolations"]]
    assert err["message"] == "\n".join(m for _, m in got)
    assert err["status"] == "INVALID_ARGUMENT"

    if body is not None:
        assert got == refused
        return

    def by_field(pairs):
        grouped = {}
        for field, message in pairs:
            grouped.setdefault(field, []).append(message)
        return grouped

    assert by_field(got) == by_field(refused)
    # each field's refusals are contiguous
    fields = [f for f, _ in got]
    assert fields == sorted(fields, key=fields.index)


@pytest.mark.parametrize(
    "params",
    [
        {"majorDimension": "rows"},
        {"majorDimension": "Rows"},
        {"majorDimension": "columns"},
        {"valueRenderOption": "unformatted_value"},
        {"valueRenderOption": "Unformatted_Value"},
        {"dateTimeRenderOption": "serial_number"},
        {"dateTimeRenderOption": "formatted_string"},
        # the name the discovery document lists first, and a `-` for a `_`
        {"majorDimension": "DIMENSION_UNSPECIFIED"},
        {"majorDimension": "dimension-unspecified"},
        # each enum by the number its name has, a sign and a leading zero allowed
        {"majorDimension": "0"},
        {"majorDimension": "+2"},
        {"valueRenderOption": "1"},
        {"valueRenderOption": "01"},
        {"dateTimeRenderOption": "1"},
        {"dateTimeRenderOption": "+1"},
    ],
)
def test_sheets_read_enums_take_a_name_in_any_case_or_its_number(base, admin_h, sheet_id, params):
    """Measured: real Sheets accepts every one of these and 400s only on a value that is not the
    enum at all (`test_sheets_values_get_rejects_a_bad_enum`), by `protojson.enum_from_string`'s
    rule."""
    assert _values(base, admin_h, sheet_id, "Sheet1!A1:A2", **params).status_code == 200


@pytest.mark.parametrize("read", ["values", "batchGet"])
@pytest.mark.parametrize(
    "sent, echoed",
    [
        ("columns", "COLUMNS"),
        ("2", "COLUMNS"),
        ("02", "COLUMNS"),
        ("DIMENSION_UNSPECIFIED", "ROWS"),
        ("0", "ROWS"),
        ("1", "ROWS"),
    ],
)
def test_sheets_values_get_echoes_the_major_dimension_by_its_name(
    base, admin_h, sheet_id, read, sent, echoed
):
    """Measured: the echo is the canonical name whatever the request used, and
    `DIMENSION_UNSPECIFIED` reads as `ROWS` (`_sheets_major`), on `values.get` and
    `values:batchGet` alike."""
    if read == "values":
        r = _values(base, admin_h, sheet_id, "Sheet1!A1:A2", majorDimension=sent)
        assert r.json()["majorDimension"] == echoed
    else:
        r = _batch(base, admin_h, sheet_id, ["Sheet1!A1:A2"], majorDimension=sent)
        assert r.json()["valueRanges"][0]["majorDimension"] == echoed


def test_sheets_values_get_render_options_agree_on_a_prose_spreadsheet(base, admin_h, sheet_id):
    """A spreadsheet whose cells are lines of stored text holds no formulas and no typed numbers —
    `spreadsheets.get` declares every cell a `stringValue` — so the three render options coincide
    on one.

    They do NOT coincide in general: a spreadsheet that STATES its grid distinguishes them, which
    is what `test_the_render_options_differ_over_typed_cells` holds this against."""
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
        for u in yaml.safe_load(live_server[1].tokens_path.read_text(encoding="utf-8"))["users"]
    }
    outsider = {"Authorization": f"Bearer {tokens['mia@acme.com']}"}  # marketing, not finance
    admin_h = {"Authorization": f"Bearer {live_server[1].admin_token}"}
    # the admin arm keeps this honest: without it a route that refused every caller would pass
    assert _values(base, admin_h, sheet_id, "Sheet1").status_code == 200
    assert _batch(base, admin_h, sheet_id, ["Sheet1"]).status_code == 200
    assert _values(base, outsider, sheet_id, "Sheet1").status_code == 403
    assert _batch(base, outsider, sheet_id, ["Sheet1"]).status_code == 403


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

# Every range-related request sent to the live Sheets API on 2026-09-12 (#174), on a workbook whose
# sheets were `Sheet1`, `Data`, `R1C1`, `RC` and `A` (1000x26 each, `a`..`f` in A1:B3 of the first
# two), with what came back: the status and the echoed `range`, or the error message — both
# notations, whitespace around the bang and inside a token, whole rows and columns past the grid,
# an empty title before the bang. The test below serves a corpus with the same five sheets and
# holds Backlot to every row, so the grammar in `_R1C1_END`'s comment is what this table says and
# not what a document says.
MEASURED_R1C1 = [
    ("R1C2", 200, "Sheet1!B1"),
    ("R1C1:R2C2", 200, "Sheet1!A1:B2"),
    ("Data!R1C1:R2C2", 200, "Data!A1:B2"),
    ("'Data'!R1C1:R2C2", 200, "Data!A1:B2"),
    ("Data!R1C1", 200, "Data!A1"),
    ("R[1]C[1]", 200, "Sheet1!B2"),
    ("Data!R[3]C[1]", 200, "Data!B4"),
    ("R[0]C[0]", 200, "Sheet1!A1"),
    ("R1C1:R[2]C[2]", 200, "Sheet1!A1:C3"),
    ("R[1]C1", 200, "Sheet1!A2"),
    ("R1C[1]", 200, "Sheet1!B1"),
    ("A1:R2C2", 400, "Unable to parse range: A1:R2C2"),
    ("R1C1:B2", 400, "Unable to parse range: R1C1:B2"),
    ("R1", 200, "Sheet1!R1"),
    ("C2", 200, "Sheet1!C2"),
    ("R1:R2", 200, "Sheet1!R1:R2"),
    ("C1:C2", 200, "Sheet1!C1:C2"),
    ("R0C0", 400, "Unable to parse range: R0C0"),
    ("R0C1", 400, "Unable to parse range: R0C1"),
    ("r1c1:r2c2", 200, "Sheet1!A1:B2"),
    ("R1C1:R1000C26", 200, "Sheet1!A1:Z1000"),
    ("R1C1:R2000C50", 200, "Sheet1!A1:Z1000"),
    ("R1001C1", 400, "Range (Sheet1!A1001) exceeds grid limits. Max rows: 1000, max columns: 26"),
    ("R1C", 200, "Sheet1!A1"),
    ("RC1", 400, "Range (Sheet1!RC1) exceeds grid limits. Max rows: 1000, max columns: 26"),
    ("R1C1:", 400, "Unable to parse range: R1C1:"),
    ("R1C1R2C2", 400, "Unable to parse range: R1C1R2C2"),
    ("Data!R1C1:R2C2:R3C3", 400, "Unable to parse range: Data!R1C1:R2C2:R3C3"),
    ("R2C", 200, "Sheet1!A2"),
    ("R1C:R3C", 200, "Sheet1!A1:A3"),
    ("RC", 200, "Sheet1!A1"),
    ("R[1]C", 200, "Sheet1!A2"),
    ("RC[1]", 200, "Sheet1!B1"),
    ("R[1]C[1]:R[2]C[2]", 200, "Sheet1!B2:C3"),
    ("RC[0]:RC[1]", 200, "Sheet1!A1:B1"),
    ("R[-1]C[0]", 400, "Unable to parse range: R[-1]C[0]"),
    ("R[0]C[-1]", 400, "Unable to parse range: R[0]C[-1]"),
    ("R[999]C[25]", 200, "Sheet1!Z1000"),
    (
        "R[1000]C[0]",
        400,
        "Range (Sheet1!A1001) exceeds grid limits. Max rows: 1000, max columns: 26",
    ),
    ("R[1]C[1]:R[3]C[1]", 200, "Sheet1!B2:B4"),
    ("R[2]C[2]:R1C1", 200, "Sheet1!A1:C3"),
    ("R2C2:R1C1", 400, "Unable to parse range: R2C2:R1C1"),
    ("R1C1:R1C1", 200, "Sheet1!A1"),
    ("R1C1:R1000C1", 200, "Sheet1!A1:A1000"),
    ("R1C1:R1C26", 200, "Sheet1!A1:Z1"),
    ("R2C1:R2C26", 200, "Sheet1!A2:Z2"),
    ("R1C0", 400, "Unable to parse range: R1C0"),
    ("R01C01", 200, "Sheet1!A1"),
    ("R[01]C[01]", 200, "Sheet1!B2"),
    ("R1 C1", 400, "Unable to parse range: R1 C1"),
    (" R1C1 ", 400, "Unable to parse range:  R1C1 "),
    ("R[+1]C[+1]", 400, "Unable to parse range: R[+1]C[+1]"),
    ("R1C1 :R2C2", 400, "Unable to parse range: R1C1 :R2C2"),
    ("A0", 400, "Unable to parse range: A0"),
    ("A1:A0", 400, "Unable to parse range: A1:A0"),
    ("Sheet1!R[1]C[1]", 200, "Sheet1!B2"),
    ("'Sheet1'!R[1]C[1]", 200, "Sheet1!B2"),
    ("Data!R1C:R2C", 200, "Data!A1:A2"),
    ("R1C1:R2", 200, "Sheet1!A1:Z2"),
    ("R1:R2C2", 200, "Sheet1!A1:B2"),
    ("1:R2C2", 400, "Unable to parse range: 1:R2C2"),
    ("A", 200, "A!A1:Z1000"),
    ("Z", 400, "Unable to parse range: Z"),
    ("A:A", 200, "Sheet1!A1:A1000"),
    ("ZZ", 400, "Unable to parse range: ZZ"),
    ("ZZ:ZZ", 400, "Range (Sheet1!ZZ) exceeds grid limits. Max rows: 1000, max columns: 26"),
    ("RC:RC", 400, "Range (Sheet1!RC) exceeds grid limits. Max rows: 1000, max columns: 26"),
    ("'RC'", 200, "'RC'!A1:Z1000"),
    ("RC!A1", 200, "'RC'!A1"),
    ("R1C1", 200, "Sheet1!A1"),
    ("'R1C1'", 200, "'R1C1'!A1:Z1000"),
    ("R1C1!A1", 200, "'R1C1'!A1"),
    ("R1C1!R1C1", 200, "'R1C1'!A1"),
    ("'A'", 200, "A!A1:Z1000"),
    ("A!A1", 200, "A!A1"),
    ("RC2", 400, "Range (Sheet1!RC2) exceeds grid limits. Max rows: 1000, max columns: 26"),
    ("RC26", 400, "Range (Sheet1!RC26) exceeds grid limits. Max rows: 1000, max columns: 26"),
    ("RC1:R2C2", 200, "Sheet1!A1:B2"),
    ("R1C1:RC", 200, "Sheet1!A1"),
    ("R1C1:C2", 200, "Sheet1!A1:B1000"),
    ("C2:R1C1", 400, "Unable to parse range: C2:R1C1"),
    ("R:R1C1", 200, "Sheet1!A1"),
    ("R2:R1C1", 400, "Unable to parse range: R2:R1C1"),
    ("R1C1:R", 200, "Sheet1!A1"),
    ("R1C1:C", 200, "Sheet1!A1"),
    ("R[1]:R[2]C[2]", 200, "Sheet1!A2:C3"),
    ("R1C1:C[1]", 200, "Sheet1!A1:B1000"),
    ("R:R", 200, "Sheet1!R1:R1000"),
    ("C:C", 200, "Sheet1!C1:C1000"),
    ("R2C1:R1C2", 400, "Unable to parse range: R2C1:R1C2"),
    ("R1C2:R2C1", 400, "Unable to parse range: R1C2:R2C1"),
    ("R[1]C[1]:R[0]C[0]", 400, "Unable to parse range: R[1]C[1]:R[0]C[0]"),
    ("R2C2:R[2]C[2]", 200, "Sheet1!B2:C3"),
    ("R[1]C[1]:R1C1", 200, "Sheet1!A1:B2"),
    ("R2C2:R[0]C[0]", 200, "Sheet1!A1:B2"),
    ("R[2]C[2]:R2C2", 200, "Sheet1!B2:C3"),
    ("R[1]C[0]:R1C1", 200, "Sheet1!A1:A2"),
    ("R1C1:R[0]C[0]", 200, "Sheet1!A1"),
    ("R[3]C[3]:R[1]C[1]", 200, "Sheet1!B2:D4"),
    ("R3C3:R[1]C[1]", 200, "Sheet1!B2:C3"),
    ("R[1]C[1]:R2C2", 200, "Sheet1!B2"),
    ("B2:A1", 200, "Sheet1!A1:B2"),
    ("B2:A2", 200, "Sheet1!A2:B2"),
    ("0:1", 400, "Unable to parse range: 0:1"),
    ("A0:B2", 400, "Unable to parse range: A0:B2"),
    ("R", 200, "Sheet1!A1"),
    ("C", 200, "Sheet1!A1"),
    ("R[1]", 200, "Sheet1!A2:Z2"),
    ("C[1]", 200, "Sheet1!B1:B1000"),
    ("R[1]:R[1]", 200, "Sheet1!A2:Z2"),
    ("C[1]:C[1]", 200, "Sheet1!B1:B1000"),
    ("R1C1:R[1]", 200, "Sheet1!A1:Z2"),
    ("R1C1:C[0]", 200, "Sheet1!A1:A1000"),
    ("R1C1:R2C", 200, "Sheet1!A1:A2"),
    ("R1C1:RC2", 200, "Sheet1!A1:B1"),
    ("R1C:R2C2", 200, "Sheet1!A1:B2"),
    ("R2:R2", 200, "Sheet1!R2"),
    ("R1C1:R1000C", 200, "Sheet1!A1:A1000"),
    ("R1C1:RC26", 200, "Sheet1!A1:Z1"),
    ("R[2]C[2]:R[0]C[0]", 200, "Sheet1!A1:C3"),
    ("R[2]C[2]:R[1]C[1]", 400, "Unable to parse range: R[2]C[2]:R[1]C[1]"),
    ("R[1]C[0]:R[0]C[0]", 400, "Unable to parse range: R[1]C[0]:R[0]C[0]"),
    ("R[0]C[1]:R[0]C[0]", 400, "Unable to parse range: R[0]C[1]:R[0]C[0]"),
    ("R[1]C[1]:R[0]C[1]", 400, "Unable to parse range: R[1]C[1]:R[0]C[1]"),
    ("R[1]C[1]:R[1]C[0]", 400, "Unable to parse range: R[1]C[1]:R[1]C[0]"),
    ("R[2]C[1]:R[1]C[2]", 400, "Unable to parse range: R[2]C[1]:R[1]C[2]"),
    ("R3C3:R2C2", 400, "Unable to parse range: R3C3:R2C2"),
    ("R[3]C[3]:R[2]C[2]", 400, "Unable to parse range: R[3]C[3]:R[2]C[2]"),
    ("Sheet1!B", 400, "Unable to parse range: Sheet1!B"),
    ("Sheet1!Z", 400, "Unable to parse range: Sheet1!Z"),
    ("Sheet1!2", 400, "Unable to parse range: Sheet1!2"),
    (" A1", 400, "Unable to parse range:  A1"),
    ("A1 ", 400, "Unable to parse range: A1 "),
    ("R3C3:R1C1", 200, "Sheet1!A1:C3"),
    ("R4C4:R1C1", 200, "Sheet1!A1:D4"),
    ("R2C1:R1C1", 400, "Unable to parse range: R2C1:R1C1"),
    ("R3C1:R1C1", 200, "Sheet1!A1:A3"),
    ("R5C5:R3C3", 200, "Sheet1!C3:E5"),
    ("R1C3:R1C1", 200, "Sheet1!A1:C1"),
    ("R1C2:R1C1", 400, "Unable to parse range: R1C2:R1C1"),
    ("R[1]C[2]:R[0]C[0]", 400, "Unable to parse range: R[1]C[2]:R[0]C[0]"),
    ("R[2]C[0]:R[0]C[0]", 200, "Sheet1!A1:A3"),
    ("R[4]C[4]:R[0]C[0]", 200, "Sheet1!A1:E5"),
    ("R[2]C[2]:R[1]C[0]", 400, "Unable to parse range: R[2]C[2]:R[1]C[0]"),
    ("R3:R1C1", 200, "Sheet1!A1:A3"),
    ("C3:R1C1", 200, "Sheet1!A1:C1"),
    ("R1000C26:R1C1", 200, "Sheet1!A1:Z1000"),
    ("R999C26:R1C1", 200, "Sheet1!A1:Z999"),
    ("R[2]:R1C1", 200, "Sheet1!A1:A3"),
    ("R[1]:R1C1", 200, "Sheet1!A1:A2"),
    ("B1:A1", 200, "Sheet1!A1:B1"),
    ("C1:A1", 200, "Sheet1!A1:C1"),
    ("A2:A1", 200, "Sheet1!A1:A2"),
    ("Sheet1! A1", 400, "Unable to parse range: Sheet1! A1"),
    ("Sheet1 !A1", 400, "Unable to parse range: Sheet1 !A1"),
    ("Sheet1! A1:B2", 400, "Unable to parse range: Sheet1! A1:B2"),
    ("'Data' !A1", 400, "Unable to parse range: 'Data' !A1"),
    ("'Data'! R1C1", 400, "Unable to parse range: 'Data'! R1C1"),
    ("Sheet1!A1 :B2", 400, "Unable to parse range: Sheet1!A1 :B2"),
    ("Sheet1!A1: B2", 400, "Unable to parse range: Sheet1!A1: B2"),
    ("1001:1001", 400, "Range (Sheet1!1001) exceeds grid limits. Max rows: 1000, max columns: 26"),
    (
        "1001:1002",
        400,
        "Range (Sheet1!1001:1002) exceeds grid limits. Max rows: 1000, max columns: 26",
    ),
    ("1000:1001", 200, "Sheet1!A1000:Z1000"),
    ("1:1001", 200, "Sheet1!A1:Z1000"),
    ("2000:2000", 400, "Range (Sheet1!2000) exceeds grid limits. Max rows: 1000, max columns: 26"),
    (
        "Sheet1!1001:1001",
        400,
        "Range (Sheet1!1001) exceeds grid limits. Max rows: 1000, max columns: 26",
    ),
    (
        "R1001:R1001",
        400,
        "Range (Sheet1!R1001) exceeds grid limits. Max rows: 1000, max columns: 26",
    ),
    (
        "R[1000]:R[1000]",
        400,
        "Range (Sheet1!1001) exceeds grid limits. Max rows: 1000, max columns: 26",
    ),
    ("Z:AA", 200, "Sheet1!Z1:Z1000"),
    ("A:ZZ", 200, "Sheet1!A1:Z1000"),
    ("AA:AB", 400, "Range (Sheet1!AA:AB) exceeds grid limits. Max rows: 1000, max columns: 26"),
    ("R1C27:R1C27", 400, "Range (Sheet1!AA1) exceeds grid limits. Max rows: 1000, max columns: 26"),
    ("C27:C27", 200, "Sheet1!C27"),
    ("C[26]", 400, "Range (Sheet1!AA) exceeds grid limits. Max rows: 1000, max columns: 26"),
    ("R1C1:R1C27", 200, "Sheet1!A1:Z1"),
    ("R1C1:R1001C1", 200, "Sheet1!A1:A1000"),
    ("1000:1000", 200, "Sheet1!A1000:Z1000"),
    ("Z:Z", 200, "Sheet1!Z1:Z1000"),
    ("1:1000", 200, "Sheet1!A1:Z1000"),
    ("R1000:R1000", 200, "Sheet1!R1000"),
    ("R1000", 200, "Sheet1!R1000"),
    ("A 1", 400, "Unable to parse range: A 1"),
    ("A1 :B2", 400, "Unable to parse range: A1 :B2"),
    ("A1: B2", 400, "Unable to parse range: A1: B2"),
    ("A1 : B2", 400, "Unable to parse range: A1 : B2"),
    ("A :A", 400, "Unable to parse range: A :A"),
    ("1 :2", 400, "Unable to parse range: 1 :2"),
    ("R 1C1", 400, "Unable to parse range: R 1C1"),
    ("R1C 1", 400, "Unable to parse range: R1C 1"),
    ("R1 C 1", 400, "Unable to parse range: R1 C 1"),
    ("R [1]C[1]", 400, "Unable to parse range: R [1]C[1]"),
    ("R[ 1]C[1]", 400, "Unable to parse range: R[ 1]C[1]"),
    ("R[1 ]C[1]", 400, "Unable to parse range: R[1 ]C[1]"),
    ("R1C1: R2C2", 400, "Unable to parse range: R1C1: R2C2"),
    ("A1\tB2", 400, "Unable to parse range: A1\tB2"),
    ("A1\t:B2", 400, "Unable to parse range: A1\t:B2"),
    ("A1  :B2", 400, "Unable to parse range: A1  :B2"),
    ("'Sheet1 '!A1", 400, "Unable to parse range: 'Sheet1 '!A1"),
    ("' Sheet1'!A1", 400, "Unable to parse range: ' Sheet1'!A1"),
    ("Sheet1!A 1", 400, "Unable to parse range: Sheet1!A 1"),
    (" ", 400, "Unable to parse range:  "),
    ("  ", 400, "Unable to parse range:   "),
    (" !A1", 400, "Unable to parse range:  !A1"),
    ("!A1", 200, "Sheet1!A1"),
    ("! A1", 400, "Unable to parse range: ! A1"),
    ("Sheet1!", 400, "Unable to parse range: Sheet1!"),
    ("Sheet1! ", 400, "Unable to parse range: Sheet1! "),
    ("!A1:B2", 200, "Sheet1!A1:B2"),
    ("!R1C1", 200, "Sheet1!A1"),
    ("!R[1]C[1]", 200, "Sheet1!B2"),
    ("!A:A", 200, "Sheet1!A1:A1000"),
    ("!Data", 400, "Unable to parse range: !Data"),
    ("!", 400, "Unable to parse range: !"),
    ("''!A1", 200, "Sheet1!A1"),
    ("!!A1", 400, "Unable to parse range: !!A1"),
    ("!Sheet1!A1", 400, "Unable to parse range: !Sheet1!A1"),
    ("Data!!A1", 400, "Unable to parse range: Data!!A1"),
]

# A subset for the SAMPLE spreadsheet, which has `Sheet1` alone: two of the five the issue's
# comment measured, then offsets, bare letters and a reversed range, on both reads.
_R1C1_ECHO = {sent: echo for sent, status, echo in MEASURED_R1C1 if status == 200}
MEASURED_ECHO_R1C1 = [
    (sent, _R1C1_ECHO[sent])
    for sent in ("R1C2", "R1C1:R2C2", "R[1]C[1]", "R1C1:R[2]C[2]", "R[1]", "C[1]", "R3C3:R1C1")
]


@pytest.mark.parametrize("rng, echo", MEASURED_ECHO_R1C1)
def test_sheets_values_r1c1_echoes_the_a1_equivalent(base, admin_h, sheet_id, rng, echo):
    """On `values.get` and on `values:batchGet`, since the reader sends the former and a client
    batching sheets sends the latter."""
    r = _values(base, admin_h, sheet_id, rng)
    assert r.status_code == 200, r.text
    assert r.json()["range"] == echo
    b = _batch(base, admin_h, sheet_id, [rng])
    assert b.status_code == 200, b.text
    assert b.json()["valueRanges"][0]["range"] == echo


def test_sheets_values_answer_every_measured_r1c1_request_as_real_does(tmp_path):
    """All MEASURED_R1C1 rows, status and echo or message alike, over a corpus with the probe's
    five sheets. One test rather than one per row: the corpus is built once, and a row that drifts
    names itself in the assertion."""
    from tests._helpers import corpus_client

    grid = [["a", "b"], ["c", "d"], ["e", "f"]]
    record = {
        "source_type": "google_drive",
        "doc_id": "probe",
        "folder": "mk",
        "title": "backlot #174 R1C1 probe",
        "author_email": "a@x.com",
        "visibility": "public",
        "subtype": "spreadsheet",
        "sheets": [{"title": t, "grid": grid} for t in ("Sheet1", "Data", "R1C1", "RC", "A")],
    }
    with corpus_client(tmp_path, [record]) as (client, settings):
        h = {"Authorization": f"Bearer {settings.admin_token}"}
        (sheet,) = client.get(
            "/drive/v3/files", headers=h, params={"q": "name = 'backlot #174 R1C1 probe'"}
        ).json()["files"]
        drifted = []
        for sent, status, shown in MEASURED_R1C1:
            r = client.get(
                f"/sheets/v4/spreadsheets/{sheet['id']}/values/{quote(sent, safe='')}", headers=h
            )
            body = r.json()
            got = body.get("range") if r.status_code == 200 else body["error"]["message"]
            if (r.status_code, got) != (status, shown):
                drifted.append(
                    f"{sent!r}: real {status} {shown!r}, Backlot {r.status_code} {got!r}"
                )
        assert drifted == [], "\n".join(drifted)


def test_sheets_values_r1c1_answers_exactly_what_its_a1_twin_does(base, admin_h, sheet_id):
    """The whole body, not only the echo: the reader's `R1C1:R{rowCount}C{columnCount}` is the
    whole grid, and has to come back as the bare sheet name does."""
    assert _values(base, admin_h, sheet_id, "R1C1:R2C2").json() == (
        _values(base, admin_h, sheet_id, "A1:B2").json()
    )
    whole = _values(base, admin_h, sheet_id, "R1C1:R1000C26").json()
    assert whole == _values(base, admin_h, sheet_id, "Sheet1").json()
    assert whole["range"] == "Sheet1!A1:Z1000"


def test_sheets_a_title_spelt_as_a_reference_is_quoted_in_either_notation():
    """Measured on sheets so named: `'A1'`, `'R1C1'` and `'RC'` echo quoted because the bare word
    reads as a cell, while `A` echoes bare — a lone column is no range. And a bare `R1` is the A1
    cell R1, not a row in R1C1 notation."""
    from backlot.routers.google import _a1_classify, _a1_title

    assert _a1_title("A1") == "'A1'"
    assert _a1_title("R1C1") == "'R1C1'"
    assert _a1_title("RC") == "'RC'"
    assert _a1_title("A") == "A"
    assert _a1_title("Data") == "Data"
    assert _a1_classify("R1")[0] == "a1" and _a1_classify("RC")[0] == "r1c1"
    assert _a1_classify("A") is None and _a1_classify("A1:R2C2") is None


def test_sheets_values_accept_a_bare_quoted_sheet_name(base, admin_h, sheet_id):
    """`'Sheet1'` with no `!cellpart` means "every cell in that sheet" — measured on a real
    spreadsheet, quoted and unquoted alike, on both `values.get` and `values:batchGet`.

    Backlot only un-quoted a title when a `!` followed, so the one form that means "the whole
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
    from backlot.routers.google import (
        _drive_facts,
        _drive_permissions,
        _drive_q_eval,
        _drive_q_parse,
    )

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
    perms = _drive_permissions(conn, served_id("google_drive", "d1"))
    # public share is type "anyone" (not "domain"), and an owner permission exists
    assert any(p["type"] == "anyone" for p in perms)
    assert any(p["role"] == "owner" for p in perms)
    # group-restricted doc surfaces a group-type permission
    gperms = _drive_permissions(conn, served_id("google_drive", "d2"))
    assert any(p["type"] == "group" for p in gperms)
    # `trashed = false` leaves a trashed file out and `trashed = true` matches it
    d2 = store.get_document(conn, "google_drive", served_id("google_drive", "d2"))
    facts = _drive_facts(d2)
    assert _drive_q_eval(_drive_q_parse("trashed = false"), facts, None, {}) is False
    assert _drive_q_eval(_drive_q_parse("trashed = true"), facts, None, {}) is True


# A file and a trashed file its owner lists, as the no-`q` branch of `drive_files_list` records.
@pytest.mark.parametrize(
    "q, listed",
    [
        (None, {"live", "gone"}),
        ("name contains 'Deck'", {"live", "gone"}),
        ("'{folder}' in parents", {"live", "gone"}),
        ("'{folder}' in parents and trashed = false", {"live"}),
        ("'{folder}' in parents and trashed = true", {"gone"}),
        ("name contains 'Deck' and not starred", {"live", "gone"}),
        ("name contains 'Deck' and trashed = false", {"live"}),
        ("name contains 'Deck' and not trashed", {"live"}),
        ("name contains 'Deck' and trashed", {"gone"}),
    ],
)
def test_drive_files_list_lists_the_trash_unless_q_leaves_it_out(tmp_path, q, listed):
    """The total is the listing's, so `nextPageToken` promises no page there is not."""
    from backlot import synth
    from tests._helpers import corpus_client

    records = [
        {
            "source_type": "google_drive",
            "doc_id": doc_id,
            "folder": "mk",
            "title": title,
            "content": "x",
            "author_email": "a@x.com",
            "visibility": "public",
            **extra,
        }
        for doc_id, title, extra in (
            ("live", "Current Deck", {}),
            ("gone", "Old Deck", {"trashed": True}),
        )
    ]
    served = {synth.gdrive_file_id(doc_id): doc_id for doc_id in ("live", "gone")}
    with corpus_client(tmp_path, records) as (client, settings):
        params = {"pageSize": 100, "q": (q or "").format(folder=synth.drive_folder_id("mk"))}
        r = client.get(
            "/drive/v3/files",
            headers={"Authorization": f"Bearer {settings.admin_token}"},
            params={k: v for k, v in params.items() if v},
        )
        assert r.status_code == 200, r.text
        assert {served[f["id"]] for f in r.json()["files"] if f["id"] in served} == listed
        assert "nextPageToken" not in r.json()


@pytest.mark.parametrize(
    "q, scope",
    [
        ("'f' in parents", "all"),
        ("('f' in parents)", "all"),
        ("'f' in parents and trashed = false", "live"),
        ("trashed = false and 'f' in parents", "live"),
        ("'f' in parents and name = 'a'", None),
        ("'f' in parents and 'me' in owners", None),
        ("'f' in parents and (name = 'a' or name = 'b')", None),
        ("'f' in parents and trashed = true", None),
        ("'f' in parents or name = 'a'", None),
        ("not 'f' in parents", None),
    ],
)
def test_drive_q_plain_folder_reads_a_folder_scope(q, scope):
    from backlot.routers import google

    assert google._drive_q_plain_folder(google._drive_q_parse(q)) == scope


@pytest.mark.parametrize(
    "q, scan, listed",
    [
        ("'{folder}' in parents", "backlot.routers.google._drive_q_rows", {"Live", "Gone"}),
        ("name = 'Gone'", "backlot.store.list_documents", {"Gone"}),
        ("name contains 'ON'", "backlot.store.list_documents", {"Gone"}),
        ("name = 'Gone' and trashed = false", "backlot.store.list_documents", set()),
        ("name contains 'e' and not trashed", "backlot.store.list_documents", {"Live"}),
    ],
)
def test_drive_a_folder_scope_or_a_name_is_listed_from_sql(tmp_path, monkeypatch, q, scan, listed):
    """`'<id>' in parents`, LlamaIndex's Google Drive reader's `q`, is served by
    `store.list_drive_folder`, and a `name` term, mirage's lookup, by `store.list_drive_by_name`,
    each with its owner's trash and whatever `trashed` term sits beside it: with ``scan`` patched
    to fail, the listing still answers, where the rows a scan builds stop at 100,000 candidates."""
    from backlot import synth

    def fail(*args, **kwargs):
        raise AssertionError(f"{q} reached {scan}")

    monkeypatch.setattr(scan, fail)
    record = {
        "source_type": "google_drive",
        "folder": "mk",
        "content": "x",
        "visibility": "private",
    }
    s = tiny_corpus(
        tmp_path,
        [
            {**record, "doc_id": "live", "title": "Live", "author_email": "mia@acme.com"},
            {**record, "doc_id": "gone", "title": "Gone", "author_email": "mia@acme.com"}
            | {"trashed": True},
        ],
    )
    tokens = yaml.safe_load(s.tokens_path.read_text(encoding="utf-8"))
    mia = {"Authorization": f"Bearer {tok(tokens, 'mia@acme.com')}"}
    params = {"q": q.format(folder=synth.drive_folder_id("mk")), "fields": "files(name)"}
    with client_for(s, reload=True) as c:
        r = c.get("/drive/v3/files", headers=mia, params=params)
        assert r.status_code == 200, r.text
        assert {f["name"] for f in r.json()["files"]} == listed


def test_drive_lists_only_the_trash_its_caller_may_see(tmp_path):
    """Mia lists her own trash, and neither Ava's private file nor the two of Ava's she can read,
    which `files.get` still answers; the admin token lists every one. Ava's file out of the trash,
    `Kept`, is listed to each caller under each `q` but `trashed`. To each of the three users
    `files.get` answers `ownedByMe: true` on exactly the trashed files it lists: `MIA@acme.com`'s is
    `mia@acme.com`'s, and `Éva@acme.com`'s is not `éva@acme.com`'s."""
    from backlot import synth

    record = {"source_type": "google_drive", "folder": "mk", "content": "alpha", "trashed": True}
    ava = {**record, "author_email": "ava@acme.com"}
    s = tiny_corpus(
        tmp_path,
        [
            {**record, "doc_id": "hers", "title": "Hers", "author_email": "mia@acme.com"},
            {**ava, "doc_id": "secret", "title": "Secret", "visibility": "private"},
            {**ava, "doc_id": "public", "title": "Public", "visibility": "public"},
            {**ava, "doc_id": "lent", "title": "Lent", "visibility": "private"}
            | {"readers": ["ava@acme.com", "mia@acme.com"]},
            {**record, "doc_id": "shout", "title": "Shout", "author_email": "MIA@acme.com"}
            | {"visibility": "private", "readers": ["MIA@acme.com", "mia@acme.com"]},
            {**record, "doc_id": "accent", "title": "Accent", "author_email": "Éva@acme.com"}
            | {"visibility": "private", "readers": ["Éva@acme.com", "éva@acme.com"]},
            {**ava, "doc_id": "kept", "title": "Kept", "visibility": "public", "trashed": False},
        ],
    )
    tokens = yaml.safe_load(s.tokens_path.read_text(encoding="utf-8"))
    callers = {"admin": tokens["admin_token"]}
    callers |= {who: tok(tokens, f"{who}@acme.com") for who in ("mia", "ava", "éva")}
    folder = synth.drive_folder_id("mk")
    queries = (None, "trashed", f"'{folder}' in parents", "fullText contains 'alpha'")
    queries += ("name contains 'e' or name contains 'u'", "name != 'x'")
    with client_for(s, reload=True) as c:
        for who, want in (
            ("admin", {"Hers", "Secret", "Public", "Lent", "Shout", "Accent"}),
            ("mia", {"Hers", "Shout"}),
            ("ava", {"Secret", "Public", "Lent"}),
            ("éva", set()),
        ):
            h = {"Authorization": f"Bearer {callers[who]}"}
            for q in queries:
                params = {"q": q, "fields": "files(name,mimeType)"} if q else {}
                r = c.get("/drive/v3/files", headers=h, params=params)
                names = {f["name"] for f in r.json()["files"] if f["mimeType"] != _FOLDER}
                assert names == (want if q == "trashed" else want | {"Kept"}), (who, q)
            for name in ("Hers", "Public", "Lent", "Shout", "Accent", "Kept"):
                r = c.get("/drive/v3/files", headers=h, params={"q": f"name = '{name}'"})
                assert {f["name"] for f in r.json()["files"]} == {name} & (want | {"Kept"}), (
                    who,
                    name,
                )
            if who == "admin":
                continue
            for doc_id in ("hers", "secret", "public", "lent", "shout", "accent"):
                url = f"/drive/v3/files/{synth.gdrive_file_id(doc_id)}"
                r = c.get(url, headers=h, params={"fields": "name,trashed,ownedByMe"})
                if r.status_code == 404:
                    continue
                body = r.json()
                assert body["trashed"] and body["ownedByMe"] == (body["name"] in want), (
                    who,
                    doc_id,
                )


#: Two folders whose synthesized times (``synth.epoch``) fall between their files', a file whose
#: time ties the `ops` folder's to the second, two files newer than every folder that tie each
#: other, stored in the opposite order to their ids, and a file only its author's token lists.
_NEWEST_FIRST_RECORDS = [
    {
        "source_type": "google_drive",
        "doc_id": doc,
        "folder": folder,
        "title": title,
        "content": content,
        "author_email": author,
        "visibility": visibility,
        "created": "2022-12-01T00:00:00Z",
        "updated": updated,
    }
    for doc, folder, title, content, author, visibility, updated in [
        ("old", "mk", "Old", "beta beta beta beta", "a@x.com", "public", "2023-01-01T00:00:00Z"),
        ("mid", "ops", "Mid", "gamma", "a@x.com", "private", "2023-03-01T00:00:00Z"),
        ("same", "ops", "Same", "gamma", "b@x.com", "public", "2023-06-06T01:18:43Z"),
        ("new", "mk", "New", "alpha beta gamma delta", "a@x.com", "public", "2024-01-01T00:00:00Z"),
        ("twin-a", "mk", "Twin A", "delta", "a@x.com", "public", "2025-01-01T00:00:00Z"),
        ("twin-b", "mk", "Twin B", "delta", "a@x.com", "public", "2025-01-01T00:00:00Z"),
    ]
]
_NEWEST_FIRST = ["Twin B", "Twin A", "New", "ops", "Same", "Mid", "mk", "Old"]


@pytest.mark.parametrize(
    "params,caller,names",
    [
        ({}, "admin", _NEWEST_FIRST),
        ({"orderBy": ""}, "admin", _NEWEST_FIRST),
        ({"q": "trashed = false"}, "admin", _NEWEST_FIRST),
        ({}, "b@x.com", ["Twin B", "Twin A", "New", "ops", "Same", "mk", "Old"]),
        ({"q": "'root' in parents"}, "admin", ["ops", "mk"]),
        ({"q": "'{mk}' in parents"}, "admin", ["Twin B", "Twin A", "New", "Old"]),
        ({"q": "name contains 'twin'"}, "admin", ["Twin B", "Twin A"]),
        (
            {"q": "mimeType != 'application/vnd.google-apps.folder'"},
            "admin",
            ["Twin B", "Twin A", "New", "Same", "Mid", "Old"],
        ),
        # the index's rank order, the older file first
        ({"q": "fullText contains 'beta'"}, "admin", ["Old", "New"]),
    ],
)
def test_drive_files_list_with_no_order_by_lists_the_newest_first(tmp_path, params, caller, names):
    """Pins the order ``_drive_newest_first`` serves and the `fullText` listing it leaves alone, at
    every page size, so each folder falls inside a page's window and outside one."""
    from backlot import synth
    from tests._helpers import corpus_client

    if "q" in params:
        params = {**params, "q": params["q"].format(mk=synth.drive_folder_id("mk"))}
    with corpus_client(tmp_path, _NEWEST_FIRST_RECORDS) as (client, settings):
        token = settings.admin_token
        if caller != "admin":
            users = client.get("/_meta/users").json()["users"]
            token = next(u["token"] for u in users if u["email"] == caller)
        h = {"Authorization": f"Bearer {token}"}
        for size in range(1, len(names) + 2):
            listed, page_token = [], None
            for _ in range(len(names) + 1):
                p = {**params, "pageSize": size, "fields": "nextPageToken,files(name)"}
                if page_token:
                    p["pageToken"] = page_token
                body = client.get("/drive/v3/files", headers=h, params=p).json()
                listed += [f["name"] for f in body["files"]]
                page_token = body.get("nextPageToken")
                if not page_token:
                    break
            assert listed == names, size


_Q_RECORDS = [
    {
        "source_type": "google_drive",
        "doc_id": "brand",
        "folder": "mk",
        "title": "Brand guidelines",
        "content": "palette",
        "author_email": "mia@x.com",
        "visibility": "public",
        "subtype": "document",
        "created": "2026-02-01T09:00:00Z",
        "updated": "2026-02-10T09:00:00Z",
    },
    {
        "source_type": "google_drive",
        "doc_id": "rev",
        "folder": "fin",
        "title": "Q1 Revenue",
        "content": "arr",
        "author_email": "cfo@x.com",
        "visibility": "public",
        "subtype": "spreadsheet",
        "created": "2026-01-05T09:00:00Z",
        "updated": "2026-01-06T09:00:00Z",
    },
    {
        "source_type": "google_drive",
        "doc_id": "deck",
        "folder": "mk",
        "title": "Q1 Deck",
        "content": "slides",
        "author_email": "mia@x.com",
        "visibility": "public",
        "subtype": "presentation",
        "created": "2026-01-20T09:00:00Z",
        "updated": "2026-01-21T09:00:00Z",
    },
    {
        "source_type": "google_drive",
        "doc_id": "notes",
        "folder": "mk",
        "title": "Mia's Notes",
        "content": "notes",
        "author_email": "mia@x.com",
        "visibility": "public",
        "subtype": "document",
        "created": "2025-12-01T09:00:00Z",
        "updated": "2025-12-02T09:00:00Z",
    },
    {
        "source_type": "google_drive",
        "doc_id": "old",
        "folder": "mk",
        "title": "Old Deck",
        "content": "stale",
        "author_email": "mia@x.com",
        "visibility": "public",
        "trashed": True,
        "created": "2025-11-01T09:00:00Z",
        "updated": "2025-11-02T09:00:00Z",
    },
]
_FOLDER = "application/vnd.google-apps.folder"


def test_drive_q_evaluates_the_operators_the_reference_lists(tmp_path):
    """Every operator the reference lists for `name`, `mimeType`, `modifiedTime`, `createdTime`,
    `viewedByMeTime`, `trashed` and `starred`, over a corpus small enough to name the answer;
    `sharedWithMe`, `fullText` and the two collections are in the client-shapes test that closes
    this group. Folders are dropped here (their `createdTime` is seeded, so a time clause's answer
    for them is not this test's to state); the test below is where they go through the same
    evaluator."""
    from tests._helpers import corpus_client

    with corpus_client(tmp_path, _Q_RECORDS) as (client, settings):
        h = {"Authorization": f"Bearer {settings.admin_token}"}

        def files(q):
            r = client.get(
                "/drive/v3/files",
                headers=h,
                params={"q": q, "fields": "files(name,mimeType)", "pageSize": 100},
            )
            assert r.status_code == 200, f"{q}: {r.text}"
            return {f["name"] for f in r.json()["files"] if f["mimeType"] != _FOLDER}

        # name: contains, =, != — `=` reads case-insensitively, as the `contains` before it did
        assert files("name = 'Q1 Deck'") == {"Q1 Deck"}
        assert files("name = 'q1 deck'") == {"Q1 Deck"}
        assert files("name != 'Q1 Deck'") == {
            "Brand guidelines",
            "Q1 Revenue",
            "Mia's Notes",
            "Old Deck",
        }
        # the reference's escape for an apostrophe inside a value
        assert files("name = 'Mia\\'s Notes'") == {"Mia's Notes"}
        # mimeType: contains, =, !=
        assert files("mimeType contains 'spreadsheet'") == {"Q1 Revenue"}
        assert files("mimeType = 'application/vnd.google-apps.presentation'") == {"Q1 Deck"}
        # modifiedTime / createdTime: the full comparison set, a zone-less value read as UTC
        assert files("modifiedTime < '2026-01-10T00:00:00Z'") == {
            "Q1 Revenue",
            "Mia's Notes",
            "Old Deck",
        }
        assert files("modifiedTime <= '2026-01-21T09:00:00Z'") == {
            "Q1 Revenue",
            "Q1 Deck",
            "Mia's Notes",
            "Old Deck",
        }
        assert files("modifiedTime >= '2026-01-21T09:00:00'") == {"Brand guidelines", "Q1 Deck"}
        assert files("modifiedTime > '2026-01-21T09:00:00Z'") == {"Brand guidelines"}
        assert files("createdTime = '2026-01-05T09:00:00Z'") == {"Q1 Revenue"}
        assert files("createdTime != '2026-01-05T09:00:00Z' and mimeType != 'x'") == {
            "Brand guidelines",
            "Q1 Deck",
            "Mia's Notes",
            "Old Deck",
        }
        # or, not, parentheses
        assert files("name contains 'Deck' or name contains 'Brand'") == {
            "Q1 Deck",
            "Brand guidelines",
            "Old Deck",
        }
        assert files("not name contains 'Q1'") == {"Brand guidelines", "Mia's Notes", "Old Deck"}
        assert files(
            "(name contains 'Q1' or name contains 'Brand') and mimeType != "
            "'application/vnd.google-apps.presentation'"
        ) == {"Q1 Revenue", "Brand guidelines"}
        # trashed: listed unless a clause leaves it out, and asked for through `not` as well as
        # `= true`
        assert files("name contains 'Deck'") == {"Q1 Deck", "Old Deck"}
        assert files("name contains 'Deck' and trashed = false") == {"Q1 Deck"}
        assert files("name contains 'Deck' and trashed = true") == {"Old Deck"}
        assert files("not trashed = false") == {"Old Deck"}
        assert files("trashed != false") == {"Old Deck"}
        # ...and through `not` beside a `name contains`, whose title-LIKE candidate set answers
        # non-trashed rows only and so must not be used here.
        assert files("name contains 'Deck' and not trashed = false") == {"Old Deck"}
        assert files("trashed") == {"Old Deck"}
        # a folder's files, the trash with them unless the scope leaves it out, and a clause
        # beside the scope applied as well
        mk = synth.drive_folder_id("mk")
        assert files(f"'{mk}' in parents") == {
            "Brand guidelines",
            "Q1 Deck",
            "Mia's Notes",
            "Old Deck",
        }
        assert files(f"'{mk}' in parents and trashed = false") == {
            "Brand guidelines",
            "Q1 Deck",
            "Mia's Notes",
        }
        assert files(f"'{mk}' in parents and trashed = false and name contains 'Deck'") == {
            "Q1 Deck"
        }
        # starred and viewedByMeTime: nothing is starred or viewed, so `!=` alone matches a time
        every = {"Brand guidelines", "Q1 Revenue", "Q1 Deck", "Mia's Notes", "Old Deck"}
        for q, expected in (
            ("starred = true", set()),
            ("starred = TRUE", set()),
            ("starred", set()),
            ("starred = false", every),
            ("starred != true", every),
            ("not starred = true", every),
            ("not starred", every),
            ("starred != false", set()),
            ("viewedByMeTime > '2000-01-01T00:00:00'", set()),
            ("viewedByMeTime < '2100-01-01T00:00:00Z'", set()),
            ("viewedByMeTime <= '2100-01-01T00:00:00Z'", set()),
            ("viewedByMeTime >= '2000-01-01'", set()),
            ("viewedByMeTime = '2000-01-01T00:00:00'", set()),
            ("viewedByMeTime != '2000-01-01T00:00:00'", every),
            ("not viewedByMeTime > '2000-01-01T00:00:00'", every),
            ("not viewedByMeTime != '2000-01-01T00:00:00'", set()),
        ):
            assert files(q) == expected, q


def test_drive_q_or_spans_folders_and_files(tmp_path):
    """The synthesized folders go through the same evaluator as stored rows, so a disjunction that
    names a folder by its mimeType and a file by its name answers both. A `'root' in parents`
    under an `or` scopes nothing — the parent shortcut reads only the terms every match must
    satisfy — so the files still come back."""
    from tests._helpers import corpus_client

    with corpus_client(tmp_path, _Q_RECORDS) as (client, settings):
        h = {"Authorization": f"Bearer {settings.admin_token}"}
        r = client.get(
            "/drive/v3/files",
            headers=h,
            params={
                "q": f"mimeType = '{_FOLDER}' or name = 'Q1 Deck'",
                "fields": "files(name)",
                "pageSize": 100,
            },
        )
        assert {f["name"] for f in r.json()["files"]} == {"mk", "fin", "Q1 Deck"}
        r = client.get(
            "/drive/v3/files",
            headers=h,
            params={"q": "'root' in parents or name contains 'Q1'", "fields": "files(name)"},
        )
        assert {f["name"] for f in r.json()["files"]} == {"mk", "fin", "Q1 Deck", "Q1 Revenue"}


def test_drive_q_refuses_a_clause_it_cannot_parse(tmp_path):
    """A term `files.list` does not take, a clause that is not the grammar, or white space alone is
    the 400 `_drive_q_refused` raises."""
    from tests._helpers import corpus_client

    with corpus_client(tmp_path, _Q_RECORDS) as (client, settings):
        h = {"Authorization": f"Bearer {settings.admin_token}"}
        for q in (
            "bogusField = 'x'",
            "name ~ 'x'",
            "name contains",
            "name contains 'unterminated",
            "trashed = 'true'",
            "modifiedTime contains 'x'",
            "modifiedTime < 'yesterday'",
            "createdTime < 'yesterday'",
            "'x' in bogus",
            "(name contains 'a'",
            "name contains 'a' name contains 'b'",
            # past `_DRIVE_Q_MAX_DEPTH`, which real reads
            "(" * 200 + "name contains 'a'" + ")" * 200,
            # refused by real beside the shapes the test below reads
            "( )",
            "()()",
            "name contains 'a' and",
            "name contains 'a' or or name contains 'b'",
            "not",
            "name contains 'a' and not",
            "not " * 200 + "name contains 'a'",
            "sharedWithMe < true",
            "sharedWithMe >= false",
            "Starred = true",
            "starred = 'true'",
            "starred = yes",
            "starred contains true",
            "VIEWEDBYMETIME > '2000-01-01T00:00:00'",
            "viewedByMeTime > 'garbage'",
            "viewedByMeTime > ''",
            "viewedByMeTime > 2020",
            "viewedByMeTime contains '2020'",
            "modifiedByMeTime > '2000-01-01T00:00:00'",
            "sharedWithMeTime > '2000-01-01T00:00:00'",
            "starred = true and",
            "not not starred",
            "not not name contains 'a'",
            "not sharedWithMe",
            "sharedWithMe = false",
            "sharedWithMe != true",
            "not (name contains 'a' and sharedWithMe)",
            "not(trashed)",
            "NOT(trashed)",
            "(not(trashed))",
            "name contains 'a' and not(starred)",
            *("trashed" + chr(cp) + "= false" for cp in (0x0B, 0x1C, 0x85, 0xA0, 0x2003, 0x3000)),
            chr(0xA0) + "trashed = false",
            *("trashed = false" + chr(cp) for cp in (0x0B, 0xA0, 0x3000)),
            " and ",
            " ",
            "\t",
            "\n",
            chr(0xA0),
        ):
            r = client.get("/drive/v3/files", headers=h, params={"q": q})
            assert r.status_code == 400, f"{q}: {r.text}"
            err = r.json()["error"]
            assert err["message"] == "Invalid Value", q
            assert (err["errors"][0]["reason"], err["errors"][0]["location"]) == ("invalid", "q")


_LIVE = {"Brand guidelines", "Q1 Revenue", "Q1 Deck", "Mia's Notes"}


@pytest.mark.parametrize(
    "q,names",
    [
        ('name = "Q1 Deck"', {"Q1 Deck"}),
        ("trashed < true", set()),
        ("trashed < false", set()),
        ("trashed <= true", {"Old Deck"}),
        ("trashed <= false", set()),
        ("trashed > true", set()),
        ("trashed > false", _LIVE),
        ("trashed >= true", {"Old Deck"}),
        ("trashed >= false", _LIVE),
        ("()", _LIVE | {"Old Deck"}),
        ("name = 'Q1 Deck' and ()", {"Q1 Deck"}),
        ("name = 'Q1 Deck' or", _LIVE | {"Old Deck"}),
        ("(name = 'Q1 Deck' or) and name contains 'Q1'", {"Q1 Deck", "Q1 Revenue"}),
        ("not (not name = 'Q1 Deck')", {"Q1 Deck"}),
        ("(" * 64 + "name = 'Q1 Deck'" + ")" * 64, {"Q1 Deck"}),
    ],
)
def test_drive_q_reads_what_real_reads_past_the_reference(tmp_path, q, names):
    """Shapes the reference does not list and real answers with a 200, each with the files its
    answer names here, folders left out."""
    from tests._helpers import corpus_client

    with corpus_client(tmp_path, _Q_RECORDS) as (client, settings):
        h = {"Authorization": f"Bearer {settings.admin_token}"}
        r = client.get(
            "/drive/v3/files",
            headers=h,
            params={"q": q, "fields": "files(name,mimeType)", "pageSize": 100},
        )
        assert r.status_code == 200, r.text
        assert {f["name"] for f in r.json()["files"] if f["mimeType"] != _FOLDER} == names


def test_drive_q_refuses_a_documented_term_it_holds_no_fact_for(tmp_path):
    """`writers`, `readers`, `visibility` and the two map-valued terms are the reference's, and a
    corpus record carries nothing to answer them from, so the refusal says so."""
    from tests._helpers import corpus_client

    with corpus_client(tmp_path, _Q_RECORDS) as (client, settings):
        h = {"Authorization": f"Bearer {settings.admin_token}"}
        for q in (
            "'mia@x.com' in writers",
            "'mia@x.com' in readers",
            "visibility = 'anyoneCanFind'",
            # The reference's own spelling for the two map-valued terms: `{` is a token, so the
            # field is read and named before the brace can be refused.
            "properties has { key='x' and value='y' }",
            "appProperties has { key='x' and value='y' }",
        ):
            r = client.get("/drive/v3/files", headers=h, params={"q": q})
            assert r.status_code == 400, f"{q}: {r.text}"
            err = r.json()["error"]
            assert "is not evaluated by Backlot" in err["message"], q
            assert err["errors"][0]["location"] == "q"


def test_drive_q_shapes_clients_send_still_parse(tmp_path):
    """The shapes clients send, spaces and all — mirage's `'<id>' in parents and trashed=false` is
    the first — plus a bare `sharedWithMe`, keywords in either case, `in owners`, and a phrase and
    an `or` around `fullText`, whose index answer is a membership test rather than a pre-filter."""
    from tests._helpers import corpus_client

    with corpus_client(tmp_path, _Q_RECORDS) as (client, settings):
        h = {"Authorization": f"Bearer {settings.admin_token}"}
        for q, expected in (
            ("'root' in parents and trashed=false", {"mk", "fin"}),
            ("mimeType='application/vnd.google-apps.folder' AND trashed=false", {"mk", "fin"}),
            # The admin token is not a Drive user, so it owns nothing and everything reads as
            # shared with it — the rule `_drive_owned_by` states.
            (
                "sharedWithMe and trashed = false",
                {"mk", "fin", "Brand guidelines", "Q1 Revenue", "Q1 Deck", "Mia's Notes"},
            ),
            (
                "(sharedWithMe = TRUE) and not (name contains 'zzzq') and trashed = false",
                {"mk", "fin", "Brand guidelines", "Q1 Revenue", "Q1 Deck", "Mia's Notes"},
            ),
            ("not (not starred)", set()),
            ("not\t(name != 'Q1 Deck')", {"Q1 Deck"}),
            ("trashed = false\fand\rname = 'Q1 Deck'", {"Q1 Deck"}),
            ("trashed = false and(name = 'Q1 Deck') \t\n", {"Q1 Deck"}),
            ("'mia@x.com' in owners and name contains 'Q1'", {"Q1 Deck"}),
            # `me` is the caller; the admin token owns nothing, so nothing is `me`'s and
            # everything non-trashed is `not me`'s.
            ("'me' in owners", set()),
            (
                "not 'me' in owners and trashed = false",
                {"mk", "fin", "Brand guidelines", "Q1 Revenue", "Q1 Deck", "Mia's Notes"},
            ),
            # `name =` is case-insensitive on the title-LIKE candidate path as `contains` is.
            ("name = 'q1 deck'", {"Q1 Deck"}),
            ("(" * 8 + "name = 'Q1 Deck'" + ")" * 8, {"Q1 Deck"}),
            ("fullText contains 'palette' and trashed = false", {"Brand guidelines"}),
            ("fullText contains '\"slides\"'", {"Q1 Deck"}),
            ("fullText contains 'palette' or name = 'Q1 Deck'", {"Brand guidelines", "Q1 Deck"}),
        ):
            r = client.get("/drive/v3/files", headers=h, params={"q": q, "fields": "files(name)"})
            assert r.status_code == 200, f"{q}: {r.text}"
            assert {f["name"] for f in r.json()["files"]} == expected, q


def test_drive_q_me_is_the_caller(tmp_path):
    """`'me' in owners` is the caller's own files and `not 'me' in owners` everyone else's, resolved
    through the identity `sharedWithMe` reads."""
    import yaml

    from tests._helpers import corpus_client

    with corpus_client(tmp_path, _Q_RECORDS) as (client, settings):
        tokens = {
            u["email"]: u["token"]
            for u in yaml.safe_load(settings.tokens_path.read_text(encoding="utf-8"))["users"]
        }
        mia = {"Authorization": f"Bearer {tokens['mia@x.com']}"}
        files = "mimeType != 'application/vnd.google-apps.folder'"
        for q, expected in (
            ("'me' in owners", {"Brand guidelines", "Q1 Deck", "Mia's Notes", "Old Deck"}),
            (f"not 'me' in owners and {files}", {"Q1 Revenue"}),
        ):
            r = client.get("/drive/v3/files", headers=mia, params={"q": q, "fields": "files(name)"})
            assert r.status_code == 200, f"{q}: {r.text}"
            assert {f["name"] for f in r.json()["files"]} == expected, q


# Four titles created in a real Drive on 2026-09-14 and the answer `files.list` gave each `q`
# against them, deleted after. `name =` folds case for ASCII letters and nothing else; `name
# contains` folds case across the alphabet and compatibility forms (the ligature, the dotted
# capital I) but neither `ß` nor accents. The same rows drive both the SQL candidate set and the
# evaluator, so this holds them to one another as much as to real.
_NAME_TITLES = ("Straße Plan", "Élan Vital", "ﬁnance deck", "backlot probe İstanbul")
MEASURED_NAME = [
    ("name = 'straße plan'", {"Straße Plan"}),
    ("name = 'STRAßE PLAN'", {"Straße Plan"}),
    ("name = 'strasse plan'", set()),
    ("name = 'STRASSE PLAN'", set()),
    ("name = 'ÉLAN VITAL'", {"Élan Vital"}),
    ("name = 'élan vital'", set()),
    ("name = 'elan vital'", set()),
    ("name = 'ﬁnance deck'", {"ﬁnance deck"}),
    ("name = 'finance deck'", set()),
    ("name contains 'straße'", {"Straße Plan"}),
    ("name contains 'strasse'", set()),
    ("name contains 'élan'", {"Élan Vital"}),
    ("name contains 'ÉLAN'", {"Élan Vital"}),
    ("name contains 'elan'", set()),
    ("name contains 'finance'", {"ﬁnance deck"}),
    ("name contains 'istanbul'", {"backlot probe İstanbul"}),
    ("name contains 'İstanbul'", {"backlot probe İstanbul"}),
]


@pytest.mark.parametrize(("q", "expected"), MEASURED_NAME, ids=[q for q, _ in MEASURED_NAME])
def test_drive_q_name_compares_as_real_drive_does(tmp_path, q, expected):
    """Every measured row, through the handler — so through `list_drive_by_name`'s candidate set
    and `_drive_q_eval` both. `name !=` is the complement of `name =` and shares its fold."""
    from tests._helpers import corpus_client

    records = [
        {
            "source_type": "google_drive",
            "doc_id": f"n{i}",
            "folder": "mk",
            "title": title,
            "content": "x",
            "author_email": "a@x.com",
            "visibility": "public",
            "subtype": "document",
            "created": "2026-01-02T09:00:00Z",
            "updated": "2026-01-03T09:00:00Z",
        }
        for i, title in enumerate(_NAME_TITLES)
    ]
    with corpus_client(tmp_path, records) as (client, settings):
        h = {"Authorization": f"Bearer {settings.admin_token}"}
        r = client.get("/drive/v3/files", headers=h, params={"q": q, "fields": "files(name)"})
        assert r.status_code == 200, r.text
        assert {f["name"] for f in r.json()["files"]} == expected
        if q.startswith("name = "):
            r = client.get(
                "/drive/v3/files",
                headers=h,
                params={
                    "q": q.replace("name = ", "name != ", 1)
                    + " and mimeType != 'application/vnd.google-apps.folder'",
                    "fields": "files(name)",
                },
            )
            assert {f["name"] for f in r.json()["files"]} == set(_NAME_TITLES) - expected


def test_drive_size_is_populated_for_docs_editors_files(tmp_path):
    """Google: `size` "is populated for files with binary content stored in Google Drive AND for
    Docs Editors files; it is not populated for shortcuts or folders." Backlot set it only in the
    binary branch, so it taught implementors that native Docs have no byte size."""
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
                "mime_type": "application/pdf",
            },
        ],
    )
    conn = store.connect_ro(s.db_path)
    native = _drive_file(
        conn, store.get_document(conn, "google_drive", served_id("google_drive", "n1"))
    )
    assert native["size"] == str(len("hello there"))
    # checksums and a download link stay binary-only, as they are on real Drive
    assert "md5Checksum" not in native and "webContentLink" not in native
    binary = _drive_file(
        conn, store.get_document(conn, "google_drive", served_id("google_drive", "b1"))
    )
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
    row = store.get_document(conn, "gmail", served_id("gmail", "m1"))
    # raw format returns the base64url RFC822 message, no parsed payload
    raw = _gmail_message(row, "raw")
    assert "raw" in raw and "payload" not in raw
    import base64

    decoded = base64.urlsafe_b64decode(raw["raw"]).decode()
    assert "Subject: Hi" in decoded and "MIME-Version: 1.0" in decoded
    # Bcc survives only on the SENDER's own copy, which is where real Gmail keeps it: a recipient's
    # copy has it stripped in transit, so a reader who is not the author must not learn who was
    # blind-copied.
    mine = _gmail_message(row, "full", "ceo@x.com")
    hdrs = {h["name"]: h["value"] for h in mine["payload"]["headers"]}
    assert hdrs["Bcc"] == "secret@x.com" and "MIME-Version" in hdrs

    theirs = _gmail_message(row, "full", "someone.else@x.com")
    assert "Bcc" not in {h["name"] for h in theirs["payload"]["headers"]}
    # and an admin/service caller (no email) is not the sender either
    assert "Bcc" not in {h["name"] for h in _gmail_message(row, "full")["payload"]["headers"]}

    # the raw RFC822 form follows the same rule -- it is the same message, serialized
    assert (
        "Bcc: secret@x.com"
        in base64.urlsafe_b64decode(_gmail_message(row, "raw", "ceo@x.com")["raw"]).decode()
    )
    assert "secret@x.com" not in decoded  # `decoded` above was fetched with no caller

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


@pytest.mark.parametrize(
    "name,value,written",
    [
        # the line real's `raw` wrote for this subject (see `_raw_header_value`)
        ("Subject", "backlot probe 회의 일정", "=?UTF-8?B?YmFja2xvdCBwcm9iZSDtmozsnZgg7J287KCV?="),
        # 58 bytes: "청" takes the 44th to 46th, so the first word ends before it
        (
            "Subject",
            "Fwd: 2026년 하반기 예산안 검토 요청드립니다",
            "=?UTF-8?B?RndkOiAyMDI264WEIO2VmOuwmOq4sCDsmIjsgrDslYgg6rKA7YagIOyalA==?= "
            "=?UTF-8?B?7LKt65Oc66a964uI64uk?=",
        ),
        ("To", "회의 <peer@x.com>", "=?UTF-8?B?7ZqM7J2Y?= <peer@x.com>"),
        (
            "To",
            '"김, 철수" <kim@x.com>, 박영희 <park@x.com>',
            "=?UTF-8?B?6rmALCDssqDsiJg=?= <kim@x.com>, =?UTF-8?B?67CV7JiB7Z2s?= <park@x.com>",
        ),
        ("To", "회의@x.com", "회의@x.com"),
        (
            "Content-Type",
            'text/plain; charset="UTF-8"; name="회의록.txt"',
            'text/plain; charset="UTF-8"; name="=?UTF-8?B?7ZqM7J2Y66GdLnR4dA==?="',
        ),
        (
            "Content-Disposition",
            'attachment; filename="회의록.txt"',
            'attachment; filename="=?UTF-8?B?7ZqM7J2Y66GdLnR4dA==?="',
        ),
    ],
)
def test_gmail_raw_writes_non_ascii_header_text_as_encoded_words(name, value, written):
    """The forms `_raw_header_value` describes, one header value each."""
    from backlot.routers.google import _raw_header_value

    assert _raw_header_value(name, value) == written


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
    row = store.get_document(conn, "gmail", served_id("gmail", "m2"))
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
    # the text and HTML parts sit in a multipart/alternative part, as `full` serves them
    alt = mime_msg.get_payload()[0]
    assert alt.get_content_type() == "multipart/alternative"
    assert [p.get_content_type() for p in alt.get_payload()] == ["text/plain", "text/html"]
    assert alt.get_payload()[0].get_payload(decode=True).decode() == "see attached"


# --- Gmail shapes measured against gmail.googleapis.com on 2026-09-30, 10-01 and 10-02 -----------

_GMAIL_SHAPES = [
    {
        "source_type": "gmail",
        "doc_id": "att",
        "mailbox": "ceo",
        "title": "With attachment",
        "content": "see attached",
        "author_email": "ceo@x.com",
        "attachments": [{"filename": "notes.txt", "mime": "text/plain", "content": "hello"}],
    },
    {
        "source_type": "gmail",
        "doc_id": "lt",
        "mailbox": "ceo",
        "title": "Angles",
        "content": "a < b & c > d",
        "author_email": "ceo@x.com",
    },
    # more than 57 bytes of UTF-8, so its base64 text/plain part runs past one 76-character line
    {
        "source_type": "gmail",
        "doc_id": "ko",
        "mailbox": "ceo",
        "title": "Non-ASCII",
        "content": "안녕하세요, 다음 주 회의 일정을 공유드립니다.",
        "author_email": "ceo@x.com",
    },
    # an HTML line on each side of the measured quoted-printable boundary (175 none, 325 QP)
    {
        "source_type": "gmail",
        "doc_id": "html175",
        "mailbox": "ceo",
        "title": "HTML line of 175",
        "content": "short",
        "html": "<div>" + "a" * 164 + "</div>",
        "author_email": "ceo@x.com",
    },
    {
        "source_type": "gmail",
        "doc_id": "html325",
        "mailbox": "ceo",
        "title": "HTML line of 325",
        "content": "short",
        "html": "<div>" + "a" * 314 + "</div>",
        "author_email": "ceo@x.com",
    },
    {
        "source_type": "gmail",
        "doc_id": "att-ko",
        "mailbox": "ceo",
        "title": "Non-ASCII attachment",
        "content": "see attached",
        "author_email": "ceo@x.com",
        "attachments": [{"filename": "notes.txt", "mime": "text/plain", "content": "안녕"}],
    },
    # a non-ASCII subject, display name and attachment name, which `raw` writes as encoded-words
    {
        "source_type": "gmail",
        "doc_id": "hdr-ko",
        "mailbox": "ceo",
        "title": "회의 일정",
        "content": "see attached",
        "author_email": "ceo@x.com",
        "to": "회의 <peer@x.com>",
        "attachments": [{"filename": "회의록.txt", "mime": "text/plain", "content": "안녕"}],
    },
    # two attachments, the second stating no `content`, so its bytes are `attachment <its own id>`:
    # as long as the stand-in for any other id, and equal to none of them
    {
        "source_type": "gmail",
        "doc_id": "att2",
        "mailbox": "ceo",
        "title": "Two attachments",
        "content": "see attached",
        "author_email": "ceo@x.com",
        "attachments": [
            {"filename": "a.txt", "mime": "text/plain", "content": "hello"},
            {"filename": "b.txt", "mime": "text/plain"},
        ],
    },
]


@pytest.fixture
def gmail_shapes(tmp_path):
    settings = tiny_corpus(tmp_path, _GMAIL_SHAPES)
    with client_for(settings, reload=True) as client:
        yield client, {"Authorization": f"Bearer {settings.admin_token}"}


def _hdrs(part) -> dict[str, str]:
    return {h["name"]: h["value"] for h in part.get("headers", [])}


def test_gmail_a_message_with_an_attachment_nests_its_text_in_multipart_alternative(gmail_shapes):
    """The part tree, and each part's headers, as measured on four web-composed messages. Those
    headers are what Gmail's composer writes and the API passes through."""
    client, h = gmail_shapes
    payload = client.get(
        f"/gmail/v1/users/me/messages/{served_id('gmail', 'att')}", headers=h
    ).json()["payload"]
    assert payload["mimeType"] == "multipart/mixed"
    alt, att = payload["parts"]
    assert (alt["partId"], alt["mimeType"]) == ("0", "multipart/alternative")
    assert list(_hdrs(alt)) == ["Content-Type"]
    assert _hdrs(alt)["Content-Type"].startswith("multipart/alternative; boundary=")
    text, html = alt["parts"]
    assert (text["partId"], text["mimeType"]) == ("0.0", "text/plain")
    # ASCII text with short lines carries no Content-Transfer-Encoding, plain or HTML
    assert _hdrs(text) == {"Content-Type": 'text/plain; charset="UTF-8"'}
    assert (html["partId"], html["mimeType"]) == ("0.1", "text/html")
    assert _hdrs(html) == {"Content-Type": 'text/html; charset="UTF-8"'}
    assert (att["partId"], att["filename"]) == ("1", "notes.txt")
    att_h = _hdrs(att)
    assert list(att_h) == [
        "Content-Type",
        "Content-Disposition",
        "Content-Transfer-Encoding",
        "X-Attachment-Id",
        "Content-ID",
    ]
    assert att_h["Content-Type"] == 'text/plain; charset="US-ASCII"; name="notes.txt"'
    assert att_h["Content-Disposition"] == 'attachment; filename="notes.txt"'
    assert att_h["Content-Transfer-Encoding"] == "base64"
    # past `f_` the value is Backlot's, so only the prefix and the Content-ID equality are pinned
    assert att_h["X-Attachment-Id"].startswith("f_")
    assert att_h["Content-ID"] == f"<{att_h['X-Attachment-Id']}>"

    # with no attachment the payload is the alternative itself, and its parts carry headers too
    plain = client.get(f"/gmail/v1/users/me/messages/{served_id('gmail', 'lt')}", headers=h).json()[
        "payload"
    ]
    assert plain["mimeType"] == "multipart/alternative"
    assert [(p["partId"], list(_hdrs(p))) for p in plain["parts"]] == [
        ("0", ["Content-Type"]),
        ("1", ["Content-Type"]),
    ]


def test_gmail_two_attachments_carry_distinct_attachment_ids(gmail_shapes):
    """Each of a message's two attachments carries its own `X-Attachment-Id`, and its `Content-ID`
    is that value in angle brackets, as measured on 2026-10-09 and 2026-10-11 over a Google
    Workspace mailbox: in each of the 11 messages whose two or more parts with an `attachmentId` all
    carried the header, no value repeated."""
    client, h = gmail_shapes
    payload = client.get(
        f"/gmail/v1/users/me/messages/{served_id('gmail', 'att2')}", headers=h
    ).json()["payload"]
    parts = [p for p in payload["parts"] if p.get("filename")]
    assert [p["filename"] for p in parts] == ["a.txt", "b.txt"]
    ids = [_hdrs(p)["X-Attachment-Id"] for p in parts]
    assert len(set(ids)) == 2, f"attachments share X-Attachment-Id: {ids}"
    for part in parts:
        part_h = _hdrs(part)
        assert part_h["Content-ID"] == f"<{part_h['X-Attachment-Id']}>"


def _cte(client, h, doc: str) -> dict[str, str | None]:
    payload = client.get(
        f"/gmail/v1/users/me/messages/{served_id('gmail', doc)}", headers=h
    ).json()["payload"]
    return {p["mimeType"]: _hdrs(p).get("Content-Transfer-Encoding") for p in payload["parts"]}


@pytest.mark.parametrize(
    ("doc", "plain", "html"),
    [
        ("lt", None, None),  # ASCII, short lines: neither part is encoded
        ("ko", "base64", "quoted-printable"),  # non-ASCII: both are
        ("html175", None, None),  # the longest ASCII HTML line measured to go unencoded
        ("html325", None, "quoted-printable"),  # the shortest measured to go quoted-printable
    ],
)
def test_gmail_text_parts_are_encoded_as_the_web_composer_encodes_them(
    gmail_shapes, doc, plain, html
):
    """Gmail's web composer chooses each part's Content-Transfer-Encoding and the API passes it
    through. Only measured points are pinned (2026-09-30, 2026-10-01, 2026-10-02): the limit between
    175 and 325 characters is Backlot's pick, and no sample had a long ASCII text/plain line."""
    client, h = gmail_shapes
    assert _cte(client, h, doc) == {"text/plain": plain, "text/html": html}


def test_gmail_a_text_attachment_names_us_ascii_or_utf8_by_its_content(gmail_shapes):
    """An ASCII attachment was served `charset="US-ASCII"` (2026-10-01) and a Korean one
    `charset="UTF-8"` (2026-09-30)."""
    client, h = gmail_shapes
    charsets = {}
    for doc in ("att", "att-ko"):
        parts = client.get(
            f"/gmail/v1/users/me/messages/{served_id('gmail', doc)}", headers=h
        ).json()["payload"]["parts"]
        charsets[doc] = _hdrs(next(p for p in parts if p["filename"]))["Content-Type"]
    assert charsets == {
        "att": 'text/plain; charset="US-ASCII"; name="notes.txt"',
        "att-ko": 'text/plain; charset="UTF-8"; name="notes.txt"',
    }


@pytest.mark.parametrize(
    "doc", ["att", "lt", "ko", "html175", "html325", "att-ko", "hdr-ko", "att2"]
)
def test_gmail_raw_and_full_describe_one_message(gmail_shapes, doc):
    """`format=raw` is ASCII, and it and the `full` payload are the same tree, with the same
    headers on the message and on each part once `raw`'s encoded-words are decoded, and each raw
    part decodes, by its own Content-Transfer-Encoding, to the bytes `full` serves, or for an
    attachment, to the bytes `attachments.get` serves for the id its `full` part names."""
    import email
    import email.policy

    client, h = gmail_shapes
    url = f"/gmail/v1/users/me/messages/{served_id('gmail', doc)}"
    full = client.get(url, headers=h).json()["payload"]
    raw = base64.urlsafe_b64decode(
        client.get(url, headers=h, params={"format": "raw"}).json()["raw"]
    )
    assert raw.isascii()
    mime = email.message_from_bytes(raw, policy=email.policy.default)
    assert not mime.defects

    def check(part, entity):
        assert entity.get_content_type() == part["mimeType"]
        assert {k: str(v) for k, v in entity.items()} == _hdrs(part)
        if "parts" in part:
            children = entity.get_payload()
            assert len(children) == len(part["parts"])
            for child, sub in zip(part["parts"], children):
                check(child, sub)
        elif "attachmentId" in part["body"]:
            got = client.get(f"{url}/attachments/{part['body']['attachmentId']}", headers=h)
            assert entity.get_payload(decode=True) == base64.urlsafe_b64decode(got.json()["data"])
        elif "data" in part["body"]:
            assert entity.get_payload(decode=True) == base64.urlsafe_b64decode(part["body"]["data"])
            if "Content-Transfer-Encoding" in entity:
                # encoded, not only labelled: RFC 2045 holds both to ASCII lines of at most 76
                body = entity.get_payload()
                assert body.isascii() and max(map(len, body.splitlines())) <= 76

    assert mime.get_content_type() == full["mimeType"]
    assert {k: str(v) for k, v in mime.items()} == _hdrs(full)
    for part, entity in zip(full["parts"], mime.get_payload(), strict=True):
        check(part, entity)


def test_gmail_metadata_payload_is_mime_type_and_headers(gmail_shapes):
    client, h = gmail_shapes
    for doc in ("att", "lt"):
        for params in (
            {"format": "metadata"},
            {"format": "metadata", "metadataHeaders": "Subject"},
        ):
            m = client.get(
                f"/gmail/v1/users/me/messages/{served_id('gmail', doc)}", headers=h, params=params
            ).json()
            assert sorted(m["payload"]) == ["headers", "mimeType"], (doc, params)


def test_gmail_metadata_headers_keeps_the_named_headers(gmail_shapes):
    """The rule the comment in `_gmail_message`'s `metadata` branch records, on `messages.get` and
    on `threads.get`, and nothing changed by the parameter without `format=metadata`."""
    client, h = gmail_shapes
    mid = served_id("gmail", "lt")
    url = f"/gmail/v1/users/me/messages/{mid}"

    def names(params, path=url):
        body = client.get(path, headers=h, params=params).json()
        payload = body["messages"][0]["payload"] if "messages" in body else body["payload"]
        return [x["name"] for x in payload["headers"]] if "headers" in payload else None

    every = names({"format": "metadata"})
    assert {"Subject", "From", "Message-ID"} <= set(every)
    for sent, want in (
        (["Subject"], ["Subject"]),
        (["subject"], ["Subject"]),
        (["MESSAGE-ID"], ["Message-ID"]),
        (["From", "subject"], [n for n in every if n in ("Subject", "From")]),
        (["Subject", "Subject"], ["Subject"]),
        (["X-Nope"], None),
        ([""], None),
        (["", "Subject"], ["Subject"]),
        ([" Subject"], None),
        (["Subject "], None),
        (["Subject,From"], None),
    ):
        assert names({"format": "metadata", "metadataHeaders": sent}) == want, sent
    thread = f"/gmail/v1/users/me/threads/{mid}"
    assert names({"format": "metadata", "metadataHeaders": "Subject"}, thread) == ["Subject"]
    full = names({})
    assert names({"metadataHeaders": "Subject"}) == full
    assert len(full) > 1


@pytest.mark.parametrize("doc", ["att", "ko", "att-ko", "att2"])
def test_gmail_a_parts_size_is_the_byte_length_of_its_data(gmail_shapes, doc):
    """The rule `_byte_len` states, over every part of the message: `att` is ASCII, where bytes and
    characters are one count, and `ko` and `att-ko` are where they differ. `att2` holds two
    attachments, each fetched by the id its own part names."""
    client, h = gmail_shapes
    url = f"/gmail/v1/users/me/messages/{served_id('gmail', doc)}"
    parts = [client.get(url, headers=h).json()["payload"]]
    while parts:
        part = parts.pop()
        parts += part.get("parts", [])
        body = part["body"]
        if "data" in body:
            assert body["size"] == len(base64.urlsafe_b64decode(body["data"])), part["mimeType"]
        elif "attachmentId" in body:
            got = client.get(f"{url}/attachments/{body['attachmentId']}", headers=h).json()
            assert sorted(got) == ["data", "size"]
            assert got["size"] == body["size"] == len(base64.urlsafe_b64decode(got["data"]))


def test_gmail_labels_list_and_get_serve_real_members(client, admin_h):
    labels = client.get("/gmail/v1/users/me/labels", headers=admin_h).json()["labels"]
    by_id = {label["id"]: label for label in labels}
    for lid in ("INBOX", "SENT", "DRAFT", "UNREAD", "STARRED", "YELLOW_STAR"):
        assert sorted(by_id[lid]) == ["id", "name", "type"], lid
    hidden = ["IMPORTANT", "CHAT", "SPAM", "TRASH"] + [
        i for i in by_id if i.startswith("CATEGORY_")
    ]
    assert len(hidden) == 9
    for lid in hidden:
        assert by_id[lid]["messageListVisibility"] == "hide", lid
        assert by_id[lid]["labelListVisibility"] == "labelHide", lid
        assert "messagesTotal" not in by_id[lid]

    counts = ["messagesTotal", "messagesUnread", "threadsTotal", "threadsUnread"]
    for lid in ("INBOX", "UNREAD", "DRAFT", "YELLOW_STAR"):
        got = client.get(f"/gmail/v1/users/me/labels/{lid}", headers=admin_h)
        assert got.status_code == 200, lid
        assert sorted(got.json()) == sorted(["id", "name", "type", *counts]), lid
    for lid in ("CHAT", "SPAM", "TRASH", "CATEGORY_SOCIAL"):
        got = client.get(f"/gmail/v1/users/me/labels/{lid}", headers=admin_h).json()
        assert sorted(got) == sorted(
            ["id", "name", "type", "messageListVisibility", "labelListVisibility", *counts]
        ), lid


def test_gmail_labels_list_order(client, admin_h):
    """The order real `labels.list` returned on one mailbox, twice, on 2026-10-01. Gmail documents
    no order, so this pins Backlot's choice, taken from that measurement, not a Gmail guarantee."""
    labels = client.get("/gmail/v1/users/me/labels", headers=admin_h).json()["labels"]
    assert [label["id"] for label in labels] == [
        "CHAT",
        "SENT",
        "INBOX",
        "IMPORTANT",
        "TRASH",
        "DRAFT",
        "SPAM",
        "CATEGORY_FORUMS",
        "CATEGORY_UPDATES",
        "CATEGORY_PERSONAL",
        "CATEGORY_PROMOTIONS",
        "CATEGORY_SOCIAL",
        "YELLOW_STAR",
        "STARRED",
        "UNREAD",
    ]


def test_gmail_a_list_with_no_match_leaves_its_array_out(client, admin_h):
    for kind in ("messages", "threads"):
        body = client.get(
            f"/gmail/v1/users/me/{kind}", headers=admin_h, params={"q": "zzqxjbacklotnomatch"}
        ).json()
        assert body == {"resultSizeEstimate": 0}, kind


def test_gmail_a_thread_carries_no_snippet(client, admin_h):
    listed = client.get("/gmail/v1/users/me/threads", headers=admin_h).json()["threads"]
    assert all("snippet" in t for t in listed)  # the list entries keep theirs
    for params in ({}, {"format": "minimal"}):
        thread = client.get(
            f"/gmail/v1/users/me/threads/{listed[0]['id']}", headers=admin_h, params=params
        ).json()
        assert sorted(thread) == ["historyId", "id", "messages"], params


def test_gmail_snippet_escapes_angle_brackets(gmail_shapes):
    """`messages.get` and `threads.list` both send `<` and `>` as `&lt;` and `&gt;`, measured on
    2026-09-30 and 2026-10-01."""
    client, h = gmail_shapes
    mid = served_id("gmail", "lt")
    m = client.get(f"/gmail/v1/users/me/messages/{mid}", headers=h).json()
    assert m["snippet"] == "a &lt; b & c &gt; d"
    threads = client.get("/gmail/v1/users/me/threads", headers=h).json()["threads"]
    assert next(t for t in threads if t["id"] == m["threadId"])["snippet"] == "a &lt; b & c &gt; d"


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


# --- the grid behind a spreadsheet: normalising it, and serialising it the way export does ------


@pytest.mark.parametrize(
    "cell,want",
    [("hello", "hello"), (42, "42"), (3.5, "3.5"), (True, "TRUE"), (False, "FALSE"), (None, "")],
)
def test_a_cell_formats_the_way_the_real_api_displays_it(cell, want):
    assert sheets_grid.formatted(cell) == want


def test_normalise_pads_short_rows_and_collapses_integral_floats():
    """A ragged grid is padded rather than refused -- a table whose last row is short is ordinary.
    3.0 collapses because a real sheet stores it as ``numberValue: 3``."""
    assert sheets_grid.normalise_grid([["a", "b", "c"], ["d"], []]) == [
        ["a", "b", "c"],
        ["d", None, None],
        [None, None, None],
    ]
    assert sheets_grid.normalise_grid([[3.0, 3.5]]) == [[3, 3.5]]
    assert sheets_grid.normalise_grid([]) == []


def test_used_extent_stops_at_the_last_row_and_column_holding_anything():
    assert sheets_grid.used_extent([["a", None, None], [None, None, None]]) == (1, 1)
    assert sheets_grid.used_extent([[None]]) == (0, 0)
    assert sheets_grid.used_extent([]) == (0, 0)


def test_csv_export_follows_the_measured_rfc4180_rules():
    """Measured against a real export: quote on a comma, a double quote or a newline; double an
    embedded quote; keep an embedded newline bare inside the quotes; leave a TAB and surrounding
    spaces unquoted; separate rows with CRLF; emit no trailing newline."""
    grid = sheets_grid.normalise_grid(
        [
            ["FIRST_SHEET_MARKER", None, None],
            ["with,comma", 'with"quote', "with\nnewline"],
            ["with\ttab", "trailing ", "  leading"],
            [42, True, None],
        ]
    )
    assert sheets_grid.to_csv(grid) == (
        "FIRST_SHEET_MARKER,,\r\n"
        '"with,comma","with""quote","with\nnewline"\r\n'
        "with\ttab,trailing ,  leading\r\n"
        "42,TRUE,"
    )


def test_tsv_export_collapses_a_newline_and_a_tab_to_a_space_and_never_quotes():
    """Measured: TSV has no quoting mechanism at all, so the conversion is lossy. Reproducing the
    loss is what agreeing with it means."""
    grid = sheets_grid.normalise_grid(
        [["with,comma", 'with"quote', "with\nnewline"], ["with\ttab", 1, None]]
    )
    assert sheets_grid.to_tsv(grid) == 'with,comma\twith"quote\twith newline\r\nwith tab\t1\t'


def test_an_empty_grid_serialises_to_the_empty_string():
    assert sheets_grid.to_csv([]) == ""
    assert sheets_grid.to_tsv([]) == ""
    assert sheets_grid.to_csv(sheets_grid.normalise_grid([[None, None]])) == ""


# --- a spreadsheet that states a real grid ------------------------------------------------------
#
# Its own corpus rather than records added to SAMPLE: SAMPLE is counted by tests across every
# vendor file, and this workbook exists to be odd -- sheet titles that collide with a cell
# reference, hold a bang, or name nothing at all.

GRID_RECORDS = [
    {
        "source_type": "google_drive",
        "doc_id": "gd-book",
        "subtype": "spreadsheet",
        "title": "Q3 pipeline",
        "folder": "sales",
        "group": "sales",
        "visibility": "public",
        "author_email": "dana@acme.com",
        "created": "2026-07-01T09:00:00Z",
        "updated": "2026-07-14T17:20:00Z",
        "sheets": [
            {"title": "Summary", "grid": [["Region", "Deals"], ["EMEA", 12, True]]},
            {"title": "Second Sheet", "grid": [["plain"]]},
            {"title": "has!bang", "grid": [["I_AM_BANG"]]},
            {"title": "A1", "grid": [["I_AM_SHEET_A1"]]},
            # A title that is a bare COLUMN reference, not a cell one: `A1` resolves inside the
            # default grid while `AB` is column 28 of a 26-column sheet, so the two fail
            # differently -- one serves the wrong cells, the other 400s.
            {"title": "AB", "grid": [["I_AM_SHEET_AB"]]},
            {"title": "Ragged", "grid": [["a", None, "c"], [None], ["z"], [None, None, "w"]]},
            {"title": "Blank", "grid": []},
        ],
    },
    {
        "source_type": "google_drive",
        "doc_id": "gd-prose",
        "subtype": "spreadsheet",
        "title": "Prose sheet",
        "folder": "sales",
        "group": "sales",
        "visibility": "public",
        "author_email": "dana@acme.com",
        "content": "month,revenue\nJan,120000",
        "created": "2026-07-01T09:00:00Z",
        "updated": "2026-07-14T17:20:00Z",
    },
    {
        "source_type": "google_drive",
        "doc_id": "gd-hostile",
        "subtype": "spreadsheet",
        "title": "Hostile cells",
        "folder": "sales",
        "group": "sales",
        "visibility": "public",
        "author_email": "dana@acme.com",
        "created": "2026-07-01T09:00:00Z",
        "updated": "2026-07-14T17:20:00Z",
        "sheets": [{"grid": [["with,comma", 'with"quote', "with\nnewline"]]}],
    },
]


@pytest.fixture(scope="module")
def grid_settings(tmp_path_factory):
    from tests._helpers import build_corpus

    return build_corpus(tmp_path_factory.mktemp("grid"), GRID_RECORDS, raw=True)


@pytest.fixture(scope="module")
def gc(grid_settings):
    """A client over the gridded corpus. ``reload`` because this module already opens one over
    SAMPLE, and the lifespan writes its connection onto the module-level app state."""
    from tests._helpers import client_for

    with client_for(grid_settings, reload=True) as c:
        yield c


@pytest.fixture(scope="module")
def gh(grid_settings):
    data = yaml.safe_load(grid_settings.tokens_path.read_text(encoding="utf-8"))
    return {"Authorization": f"Bearer {data['admin_token']}"}


@pytest.fixture(scope="module")
def book(grid_settings):
    from tests._helpers import served_id

    return served_id("google_drive", "gd-book")


@pytest.fixture(scope="module")
def prose(grid_settings):
    from tests._helpers import served_id

    return served_id("google_drive", "gd-prose")


@pytest.fixture(scope="module")
def hostile(grid_settings):
    from tests._helpers import served_id

    return served_id("google_drive", "gd-hostile")


def test_a_prose_spreadsheet_is_still_one_synthesized_sheet(gc, gh, prose):
    """The prose representation becomes one more grid rather than a second code path, so its served
    shape must not move: one sheet, `sheetId` 0, the title Backlot has always given it."""
    props = gc.get(f"/sheets/v4/spreadsheets/{prose}", headers=gh).json()["sheets"]
    assert len(props) == 1
    assert props[0]["properties"] == {
        "sheetId": 0,
        "title": "Sheet1",
        "index": 0,
        "sheetType": "GRID",
        "gridProperties": {"rowCount": 1000, "columnCount": 26},
    }


def test_a_gridded_spreadsheet_reports_one_entry_per_stated_sheet(gc, gh, book):
    """Measured on a real workbook: only the sheet created with the spreadsheet is 0, and every
    later one carries a large pseudo-random integer -- so nothing may read `sheetId` as an index."""
    props = [
        s["properties"]
        for s in gc.get(f"/sheets/v4/spreadsheets/{book}", headers=gh).json()["sheets"]
    ]
    assert [p["title"] for p in props] == [
        "Summary",
        "Second Sheet",
        "has!bang",
        "A1",
        "AB",
        "Ragged",
        "Blank",
    ]
    assert [p["index"] for p in props] == [0, 1, 2, 3, 4, 5, 6]
    assert props[0]["sheetId"] == 0
    assert all(p["sheetId"] > 0 for p in props[1:])
    assert len({p["sheetId"] for p in props}) == len(props)


def test_a_sheet_wider_or_taller_than_the_default_grid_widens_its_grid_properties(gc, gh, book):
    """Measured: the API refuses a write outside the grid rather than growing it, so a grid is
    never smaller than the data it holds. The default is 1000 x 26 either way."""
    props = {
        s["properties"]["title"]: s["properties"]["gridProperties"]
        for s in gc.get(f"/sheets/v4/spreadsheets/{book}", headers=gh).json()["sheets"]
    }
    assert props["Summary"] == {"rowCount": 1000, "columnCount": 26}
    assert props["Blank"] == {"rowCount": 1000, "columnCount": 26}


def _gvalues(gc, gh, book, rng, **params):
    return gc.get(
        f"/sheets/v4/spreadsheets/{book}/values/{quote(rng, safe='')}", headers=gh, params=params
    )


@pytest.mark.parametrize(
    "spec,echo",
    [
        ("Summary!A1:B2", "Summary!A1:B2"),
        ("summary!A1:B2", "Summary!A1:B2"),  # lookup is case-insensitive, the echo normalises
        ("'Second Sheet'!A1", "'Second Sheet'!A1"),
        ("Second Sheet!A1", "'Second Sheet'!A1"),  # an unquoted name with a space is accepted
        ("has!bang!A1", "'has!bang'!A1"),  # the separator is the LAST bang
        ("has!bang", "'has!bang'!A1:Z1000"),  # a bare sheet name is the whole declared grid
        ("A1", "Summary!A1"),  # a cell reference beats a sheet named A1
        ("'A1'!A1", "'A1'!A1"),
        ("A1!A1", "'A1'!A1"),
        ("Summary", "Summary!A1:Z1000"),
        ("Summary!A:B", "Summary!A1:B1000"),  # an open range fills from gridProperties
        ("Summary!1:2", "Summary!A1:Z2"),
        ("Summary!B2:B1", "Summary!B1:B2"),  # reversed normalises ascending
        ("Summary!A999:B1002", "Summary!A999:B1000"),  # the END clamps to the grid
    ],
)
def test_an_a1_range_resolves_and_echoes_the_way_the_real_api_does(gc, gh, book, spec, echo):
    r = _gvalues(gc, gh, book, spec)
    assert r.status_code == 200, r.text
    assert r.json()["range"] == echo


@pytest.mark.parametrize("spec", ["Nope!A1:B2", "Nope", "!!!", "Summary!"])
def test_an_unknown_sheet_gets_the_same_message_unparseable_garbage_gets(gc, gh, book, spec):
    """Measured: the real API answers `Unable to parse range: <the range verbatim>` for a sheet it
    does not have, which is the same message it gives genuine garbage. Not localised."""
    r = _gvalues(gc, gh, book, spec)
    assert r.status_code == 400
    assert r.json()["error"]["message"] == f"Unable to parse range: {spec}"
    assert r.json()["error"]["status"] == "INVALID_ARGUMENT"


def test_an_unqualified_range_answers_from_the_sheet_at_index_zero(gc, gh, book):
    assert _gvalues(gc, gh, book, "A1:B1").json()["values"] == [["Region", "Deals"]]


def test_a_bare_sheet_name_reads_that_sheet_not_the_first(gc, gh, book):
    assert _gvalues(gc, gh, book, "has!bang").json()["values"] == [["I_AM_BANG"]]
    assert _gvalues(gc, gh, book, "'A1'!A1").json()["values"] == [["I_AM_SHEET_A1"]]


# --- typed cells ---------------------------------------------------------------------------


def test_a_typed_cell_carries_all_three_value_fields(gc, gh, book):
    r = gc.get(
        f"/sheets/v4/spreadsheets/{book}",
        headers=gh,
        params={"includeGridData": "true", "ranges": "Summary!A2:C2"},
    )
    cells = r.json()["sheets"][0]["data"][0]["rowData"][0]["values"]
    # the format each type carries is its own test; here the three VALUE fields are the subject
    assert cells[0] == {
        "userEnteredValue": {"stringValue": "EMEA"},
        "effectiveValue": {"stringValue": "EMEA"},
        "formattedValue": "EMEA",
        "effectiveFormat": {**_CELL_FORMAT, "horizontalAlignment": "LEFT"},
    }
    assert cells[1] == {
        "userEnteredValue": {"numberValue": 12},
        "effectiveValue": {"numberValue": 12},
        "formattedValue": "12",
        "effectiveFormat": {**_CELL_FORMAT, "horizontalAlignment": "RIGHT"},
    }
    assert cells[2] == {
        "userEnteredValue": {"boolValue": True},
        "effectiveValue": {"boolValue": True},
        "formattedValue": "TRUE",
        "effectiveFormat": {**_CELL_FORMAT, "horizontalAlignment": "CENTER"},
    }


_GRID_KEYS = ["rowData", "rowMetadata", "columnMetadata"]
_RAGGED_ROWS = [["a", {}, "c"], {}, ["z"], [{}, {}, "w"]]


@pytest.mark.parametrize(
    "read, rng, keys, rows",
    [
        ("get", "Ragged!A1:D5", _GRID_KEYS, _RAGGED_ROWS),
        ("filter", "Ragged!A1:D5", _GRID_KEYS, _RAGGED_ROWS),
        ("get", "Ragged", _GRID_KEYS, _RAGGED_ROWS),
        ("get", "Ragged!B1:D1", ["startColumn", *_GRID_KEYS], [[{}, "c"]]),
        ("get", "Ragged!B1:D4", ["startColumn", *_GRID_KEYS], [[{}, "c"], {}, {}, [{}, "w"]]),
        ("get", "Ragged!A2:B3", ["startRow", *_GRID_KEYS], [{}, ["z"]]),
        ("get", "Ragged!B2:D4", ["startRow", "startColumn", *_GRID_KEYS], [{}, {}, [{}, "w"]]),
        ("filter", "Ragged!B2:D4", ["startRow", "startColumn", *_GRID_KEYS], [{}, {}, [{}, "w"]]),
        ("get", "Ragged!A1:B2", _GRID_KEYS, [["a"]]),
        ("filter", "Ragged!A1:B2", _GRID_KEYS, [["a"]]),
        ("get", "Ragged!C1:C3", ["startColumn", *_GRID_KEYS], [["c"]]),
        ("get", "Ragged!B1:B4", ["startColumn", "rowMetadata", "columnMetadata"], []),
        ("get", "Blank", ["rowMetadata", "columnMetadata"], []),
        ("filter", "Blank", ["rowMetadata", "columnMetadata"], []),
        # A `gridRange` holding no cell, on the first sheet.
        ("filter", {"startRowIndex": 1, "endRowIndex": 1}, ["startRow", "columnMetadata"], []),
        (
            "filter",
            {"startColumnIndex": 1, "endColumnIndex": 1},
            ["startColumn", "rowMetadata"],
            [],
        ),
        (
            "filter",
            {"startRowIndex": 2, "endRowIndex": 2, "startColumnIndex": 1, "endColumnIndex": 4},
            ["startRow", "startColumn", "columnMetadata"],
            [],
        ),
        (
            "filter",
            {"startRowIndex": 1, "endRowIndex": 4, "startColumnIndex": 2, "endColumnIndex": 2},
            ["startRow", "startColumn", "rowMetadata"],
            [],
        ),
        ("filter", {"endRowIndex": 0}, ["columnMetadata"], []),
    ],
)
def test_a_grid_data_block_serves_reals_keys_and_rows(gc, gh, book, read, rng, keys, rows):
    """Each block as real Sheets served it from a sheet laid out like `Ragged` or `Blank`,
    measured as `_sheets_grid_data` records, and for a `gridRange` holding no cell as
    `_sheets_empty_grid_data` records. A cell is written here as its `formattedValue`, and an empty
    cell or a row holding no value as the `{}` served for it."""
    if read == "get":
        r = gc.get(
            f"/sheets/v4/spreadsheets/{book}",
            headers=gh,
            params={"includeGridData": "true", "ranges": rng},
        )
    else:
        r = gc.post(
            f"/sheets/v4/spreadsheets/{book}:getByDataFilter",
            headers=gh,
            json={
                "dataFilters": [{"gridRange": rng} if isinstance(rng, dict) else {"a1Range": rng}],
                "includeGridData": True,
            },
        )
    assert r.status_code == 200, r.text
    block = r.json()["sheets"][0]["data"][0]
    assert list(block) == keys
    got = [
        [c["formattedValue"] if c else {} for c in row["values"]] if "values" in row else row
        for row in block.get("rowData", [])
    ]
    assert got == rows


@pytest.mark.parametrize(
    "render,want",
    [
        ("FORMATTED_VALUE", [["EMEA", "12", "TRUE"]]),
        # measured: UNFORMATTED_VALUE returns real JSON numbers and booleans, and FORMULA returns
        # the identical raw value for every cell that is not a formula
        ("UNFORMATTED_VALUE", [["EMEA", 12, True]]),
        ("FORMULA", [["EMEA", 12, True]]),
        # by number, in the order `_PJ_RENDER` lists them
        ("0", [["EMEA", "12", "TRUE"]]),
        ("1", [["EMEA", 12, True]]),
        ("2", [["EMEA", 12, True]]),
        # a repeated option is read from its LAST repeat (gerr.first_repeat)
        (["FORMATTED_VALUE", "UNFORMATTED_VALUE"], [["EMEA", 12, True]]),
        (["UNFORMATTED_VALUE", "FORMATTED_VALUE"], [["EMEA", "12", "TRUE"]]),
    ],
)
def test_the_render_options_differ_over_typed_cells(gc, gh, book, render, want):
    assert (
        _gvalues(gc, gh, book, "Summary!A2:C2", valueRenderOption=render).json()["values"] == want
    )


@pytest.mark.parametrize("render", ["FORMATTED_VALUE", "UNFORMATTED_VALUE", "FORMULA"])
def test_an_empty_cell_is_the_empty_string_under_every_render_option(gc, gh, book, render):
    """Measured: a JSON string even under UNFORMATTED_VALUE, never null."""
    got = _gvalues(gc, gh, book, "Ragged!A1:C1", valueRenderOption=render).json()["values"]
    assert got == [["a", "", "c"]]


def test_columns_transposes_a_real_two_dimensional_block(gc, gh, book):
    got = _gvalues(gc, gh, book, "Summary!A1:B2", majorDimension="COLUMNS").json()["values"]
    assert got == [["Region", "EMEA"], ["Deals", "12"]]


def test_columns_keeps_a_fully_empty_interior_column_as_an_empty_list(gc, gh, book):
    got = _gvalues(gc, gh, book, "Ragged!A1:C1", majorDimension="COLUMNS").json()["values"]
    assert got == [["a"], [], ["c"]]


def test_rows_are_trimmed_per_row_so_they_come_back_ragged(gc, gh, book):
    got = _gvalues(gc, gh, book, "Summary!A1:C2").json()["values"]
    assert got == [["Region", "Deals"], ["EMEA", "12", "TRUE"]]


def test_an_entirely_empty_range_has_no_values_key(gc, gh, book):
    assert "values" not in _gvalues(gc, gh, book, "Summary!H1:I3").json()
    assert "values" not in _gvalues(gc, gh, book, "Blank").json()


def test_one_unparseable_range_fails_the_whole_batch(gc, gh, book):
    r = gc.get(
        f"/sheets/v4/spreadsheets/{book}/values:batchGet",
        headers=gh,
        params={"ranges": ["Summary!A1:B2", "Nope!A1"]},
    )
    assert r.status_code == 400
    assert r.json()["error"]["message"] == "Unable to parse range: Nope!A1"


def test_batch_get_reads_several_sheets_of_one_workbook(gc, gh, book):
    r = gc.get(
        f"/sheets/v4/spreadsheets/{book}/values:batchGet",
        headers=gh,
        params={"ranges": ["Summary!A1:A1", "'Second Sheet'!A1"], "valueRenderOption": "FORMULA"},
    )
    assert [v["range"] for v in r.json()["valueRanges"]] == [
        "Summary!A1",
        "'Second Sheet'!A1",
    ]


def test_batch_get_transposes_a_real_two_dimensional_block(gc, gh, book):
    """`test_sheets_batch_get_honors_major_dimension` asks for COLUMNS over a single column, where
    a transpose and a no-op that wraps the column in a list are the same answer. This is the range
    that tells them apart."""
    r = gc.get(
        f"/sheets/v4/spreadsheets/{book}/values:batchGet",
        headers=gh,
        params={"ranges": ["Summary!A1:B2"], "majorDimension": "COLUMNS"},
    )
    got = r.json()["valueRanges"][0]
    assert got["majorDimension"] == "COLUMNS"
    assert got["values"] == [["Region", "EMEA"], ["Deals", "12"]]


def test_include_grid_data_without_ranges_gives_every_sheet_its_own_cells(gc, gh, book):
    """Measured: with the flag and no `ranges`, every sheet carries a block of its own.

    A sheet must not be addressed by round-tripping its title back through the A1 parser -- a title
    that reads as a cell reference (`A1`) or a column reference (`AB`) would then resolve against
    the FIRST sheet, serving one sheet's cells under another's name, or overflowing the grid and
    failing the whole call."""
    r = gc.get(f"/sheets/v4/spreadsheets/{book}", headers=gh, params={"includeGridData": "true"})
    assert r.status_code == 200, r.text
    got = {}
    for s in r.json()["sheets"]:
        rows = s["data"][0].get("rowData", [])
        # The cells holding a value are what say which sheet answered; an empty one is `{}`.
        got[s["properties"]["title"]] = [
            [v["formattedValue"] for v in row.get("values", []) if v] for row in rows
        ]
    assert got["A1"] == [["I_AM_SHEET_A1"]]
    assert got["AB"] == [["I_AM_SHEET_AB"]]
    assert got["has!bang"] == [["I_AM_BANG"]]
    assert got["Summary"][0] == ["Region", "Deals"]
    assert got["Blank"] == []


# --- ranges scopes the sheets array ----------------------------------------------------------


def test_ranges_filters_the_sheets_array_itself(gc, gh, book):
    """Measured: a sheet no range touches is absent from the response entirely, not merely served
    without data."""
    r = gc.get(
        f"/sheets/v4/spreadsheets/{book}",
        headers=gh,
        params={"includeGridData": "true", "ranges": "Summary!A1:B2"},
    )
    assert [s["properties"]["title"] for s in r.json()["sheets"]] == ["Summary"]


def test_two_ranges_on_one_sheet_give_it_two_data_blocks(gc, gh, book):
    r = gc.get(
        f"/sheets/v4/spreadsheets/{book}",
        headers=gh,
        params={"includeGridData": "true", "ranges": ["Summary!A1:A2", "Summary!B1:C2"]},
    )
    sheets = r.json()["sheets"]
    assert len(sheets) == 1
    blocks = sheets[0]["data"]
    assert len(blocks) == 2
    assert "startColumn" not in blocks[0]  # proto3 drops a zero default
    assert blocks[1]["startColumn"] == 1
    # the metadata is the BLOCK's size, not the grid's, once `ranges` scopes it — measured 2 and 2
    # for A1:B2 against a sheet whose unscoped block carries 1000 and 26
    assert len(blocks[0]["rowMetadata"]) == 2 and len(blocks[0]["columnMetadata"]) == 1
    assert len(blocks[1]["rowMetadata"]) == 2 and len(blocks[1]["columnMetadata"]) == 2


def test_ranges_across_two_sheets_returns_both_in_index_order(gc, gh, book):
    r = gc.get(
        f"/sheets/v4/spreadsheets/{book}",
        headers=gh,
        params={"includeGridData": "true", "ranges": ["'A1'!A1", "Summary!A1"]},
    )
    assert [s["properties"]["title"] for s in r.json()["sheets"]] == ["Summary", "A1"]


def test_ranges_without_include_grid_data_still_filters_and_serves_no_cells(gc, gh, book):
    r = gc.get(f"/sheets/v4/spreadsheets/{book}", headers=gh, params={"ranges": "Summary!A1:B2"})
    sheets = r.json()["sheets"]
    assert [s["properties"]["title"] for s in sheets] == ["Summary"]
    assert "data" not in sheets[0]


# --- Drive export must keep agreeing with the Sheets API --------------------------------------


def _export(gc, gh, fid, mime):
    return gc.get(f"/drive/v3/files/{fid}/export", headers=gh, params={"mimeType": mime})


def test_csv_export_of_a_gridded_spreadsheet_serialises_its_first_sheet(gc, gh, book):
    """Measured: only the FIRST sheet is exported, from `formattedValue`. `content` is derived from
    that sheet at import, so the route needs no branch for CSV."""
    # Three fields per row, not two: rows are rectangular to the sheet's last USED column, which
    # row 2 puts at C. `values.get` trims each row instead, so it answers ragged.
    assert _export(gc, gh, book, "text/csv").text == "Region,Deals,\r\nEMEA,12,TRUE"


@pytest.mark.parametrize("mime", ["text/tab-separated-values", "TEXT/TAB-SEPARATED-VALUES"])
def test_tsv_export_reserialises_the_grid_rather_than_serving_content(gc, gh, hostile, mime):
    """`content` is the first sheet's CSV, so serving it for TSV too would answer commas and
    quotes. The lossy space-substitution is `sheets_grid.to_tsv`'s own test; this is the route
    reaching it, spelled either way `drive_files_export` accepts."""
    r = _export(gc, gh, hostile, mime)
    assert r.text == 'with,comma\twith"quote\twith newline'
    assert r.headers["content-type"] == mime


def test_a_prose_spreadsheet_still_exports_verbatim(gc, gh, prose):
    for mime in ("text/csv", "text/tab-separated-values"):
        assert _export(gc, gh, prose, mime).text == "month,revenue\nJan,120000"


def _first_sheet_values(gc, gh, fid):
    first = gc.get(f"/sheets/v4/spreadsheets/{fid}", headers=gh).json()["sheets"][0]["properties"][
        "title"
    ]
    r = gc.get(f"/sheets/v4/spreadsheets/{fid}/values/{quote(first, safe='')}", headers=gh)
    return r.json().get("values", [])


@pytest.mark.parametrize("which", ["book", "hostile"])
def test_csv_export_of_a_grid_parses_back_into_the_cells_the_sheets_api_serves(
    gc, gh, request, which
):
    """The trap #35 was filed for, re-opened by the grid: the two APIs must not describe one
    document two ways. Export rows are rectangular to the last used column while `values.get` trims
    each row, so the comparison pads -- that difference is measured, not a disagreement."""
    fid = request.getfixturevalue(which)
    exported = list(csv.reader(io.StringIO(_export(gc, gh, fid, "text/csv").text)))
    served = _first_sheet_values(gc, gh, fid)
    width = max((len(r) for r in served), default=0)
    assert exported == [r + [""] * (width - len(r)) for r in served]


def test_a_prose_export_round_trips_through_its_lines_not_through_a_csv_parser(gc, gh, prose):
    """The prose path agrees DIFFERENTLY, and asserting the gridded property over it would be
    asserting the thing #35 refused.

    A prose spreadsheet's cells are its LINES -- `_sheets_grid` splits on nothing, because 82.6% of
    real spreadsheet records are prose and comma-splitting manufactures columns out of sentence
    punctuation. So its export is the lines joined back, byte for byte, and running a CSV parser
    over it would find columns the Sheets API never claimed were there. Both APIs still describe
    one document; the shape they agree on is the line, not the field."""
    exported = _export(gc, gh, prose, "text/csv").text
    served = _first_sheet_values(gc, gh, prose)
    assert all(len(row) == 1 for row in served)
    assert exported == "\n".join(row[0] for row in served)
    # and the parser reading really would disagree -- which is why it is not asserted above
    assert list(csv.reader(io.StringIO(exported))) != served


# --- the standard query parameters every Sheets read accepts ------------------------------------


@pytest.mark.parametrize(
    "mask, want",
    [
        ("range", {"range": "Summary!A1:B2"}),
        ("majorDimension", {"majorDimension": "ROWS"}),
        ("range,majorDimension", {"range": "Summary!A1:B2", "majorDimension": "ROWS"}),
        # a trailing comma is tolerated, measured
        ("range,", {"range": "Summary!A1:B2"}),
        # a run of `*` under a field holding no message is all of it
        ("range.*.*", {"range": "Summary!A1:B2"}),
        # `*` beside a name is dropped
        ("range,*", {"range": "Summary!A1:B2"}),
        ("*", None),
    ],
)
def test_fields_narrows_a_value_range(gc, gh, book, mask, want):
    r = gc.get(
        f"/sheets/v4/spreadsheets/{book}/values/Summary!A1:B2", headers=gh, params={"fields": mask}
    )
    assert r.status_code == 200
    assert r.json() == (want if want is not None else r.json()) and (
        want is None or set(r.json()) == set(want)
    )


_BOOK_KEYS = ["spreadsheetId", "properties", "sheets", "spreadsheetUrl"]


@pytest.mark.parametrize(
    "mask, want",
    [
        ("spreadsheetId", ["spreadsheetId"]),
        ("properties.title", ["properties"]),
        # `.` and `/` both descend, and `a(b,c)` groups — measured, all three spellings work
        ("properties/title", ["properties"]),
        ("properties(title)", ["properties"]),
        ("spreadsheetId,sheets.properties.index", ["spreadsheetId", "sheets"]),
        # the response's order, not the mask's
        ("sheets.properties.index,spreadsheetId", ["spreadsheetId", "sheets"]),
        # declared, and held by no spreadsheet here
        ("namedRanges", []),
        ("developerMetadata,dataSources", []),
        ("properties.iterativeCalculationSettings", ["properties"]),
        ("commentsViewMode", ["commentsViewMode"]),
        # `*` alone is no mask, and beside a name is dropped
        ("*", _BOOK_KEYS),
        ("(*)", _BOOK_KEYS),
        (",", _BOOK_KEYS),
        ("spreadsheetId,*", ["spreadsheetId"]),
        ("*.*", [*_BOOK_KEYS, "commentsViewMode"]),
        ("*.title", ["properties"]),
        # a run of `*` under a field holding no message is all of it
        ("*.*.*", [*_BOOK_KEYS, "commentsViewMode"]),
        ("spreadsheetId.*.*", ["spreadsheetId"]),
        # an empty group names nothing
        ("a()", _BOOK_KEYS),
        ("spreadsheetId,sheets()", ["spreadsheetId"]),
        ("sheets(merges,conditionalFormats)", {"sheets": [{}]}),
        ("sheets.properties.hidden", {"sheets": [{"properties": {}}]}),
        ("sheets.*.title", {"sheets": [{"properties": {"title": "Ragged"}}]}),
        # a narrower path wins over a wider one and over `*`
        ("sheets,sheets.properties.title", {"sheets": [{"properties": {"title": "Ragged"}}]}),
        ("sheets.*,sheets.properties.title", {"sheets": [{"properties": {"title": "Ragged"}}]}),
        # a cell left empty by the mask is cut as an empty cell is
        (
            "sheets.data.rowData.values.userEnteredValue",
            {
                "sheets": [
                    {
                        "data": [
                            {
                                "rowData": [
                                    {
                                        "values": [
                                            {"userEnteredValue": {"stringValue": "a"}},
                                            {},
                                            {"userEnteredValue": {"stringValue": "c"}},
                                        ]
                                    },
                                    {},
                                    {"values": [{"userEnteredValue": {"stringValue": "z"}}]},
                                    {
                                        "values": [
                                            {},
                                            {},
                                            {"userEnteredValue": {"stringValue": "w"}},
                                        ]
                                    },
                                ]
                            }
                        ]
                    }
                ]
            },
        ),
        (
            "sheets.data.rowData.values.effectiveValue.formulaValue",
            {
                "sheets": [
                    {
                        "data": [
                            {
                                "rowData": [
                                    {
                                        "values": [
                                            {"effectiveValue": {}},
                                            {},
                                            {"effectiveValue": {}},
                                        ]
                                    },
                                    {},
                                    {"values": [{"effectiveValue": {}}]},
                                    {"values": [{}, {}, {"effectiveValue": {}}]},
                                ]
                            }
                        ]
                    }
                ]
            },
        ),
        ("sheets.data.rowData.values.note", {"sheets": [{"data": [{}]}]}),
        ("sheets.data.rowData.values.userEnteredFormat", {"sheets": [{"data": [{}]}]}),
        (
            "sheets.data.rowMetadata.hiddenByUser",
            {"sheets": [{"data": [{"rowMetadata": [{}, {}, {}, {}]}]}]},
        ),
    ],
)
def test_fields_narrows_a_spreadsheet(gc, gh, book, mask, want):
    """Each row as real answered it, as `_gmask_select` records: its keys, or for a row whose
    `want` is a body, the whole answer over `Ragged`, whose rows hold a gap, nothing at all, one
    cell and a cell after two gaps, as `_gmask_tree` and `_gmask_trim_rows` read it."""
    params = {"fields": mask} | ({"ranges": "Ragged!A1:C4"} if isinstance(want, dict) else {})
    r = gc.get(f"/sheets/v4/spreadsheets/{book}", headers=gh, params=params)
    assert r.status_code == 200, r.text
    assert (r.json() if isinstance(want, dict) else list(r.json())) == want


def test_fields_reaches_through_the_sheets_list(gc, gh, book):
    """A mask projects over a list rather than indexing it, so one path reaches every sheet."""
    r = gc.get(
        f"/sheets/v4/spreadsheets/{book}",
        headers=gh,
        params={"fields": "sheets(properties(title,index))"},
    )
    got = r.json()["sheets"]
    assert got[0] == {"properties": {"title": "Summary", "index": 0}}
    assert all(set(s["properties"]) == {"title", "index"} for s in got)


@pytest.mark.parametrize(
    "url, mask, field, path",
    [
        ("{book}", "nope", "nope", "nope"),
        ("{book}", "sheets.properties.nope", "sheets.properties.nope", "sheets.properties.nope"),
        ("{book}", "SpreadsheetId", "SpreadsheetId", "SpreadsheetId"),
        ("{book}", "spreadsheetId,bogus", "spreadsheetId,bogus", "bogus"),
        (
            "{book}",
            "sheets(properties/bogus)",
            "sheets(properties.bogus)",
            "sheets.properties.bogus",
        ),
        ("{book}", "sheets.properties.title,bogus2,sheets.bogus3", None, "bogus2"),
        ("{book}", "bogus.x", "bogus.x", "bogus.x"),
        ("{book}", "namedRanges.bogus", "namedRanges.bogus", "namedRanges.bogus"),
        # a map's keys are not names
        ("{book}", "sheets.filterViews.criteria.foo", None, "sheets.filterViews.criteria.foo"),
        ("{book}", "sheets.*.bogus", "sheets.*.bogus", "sheets.*.bogus"),
        ("{book}", "*.*.rowCount", "*.*.rowCount", "*.*.rowCount"),
        ("{book}", "spreadsheetId.*.x", "spreadsheetId.*.x", "spreadsheetId.*.x"),
        ("{book}/values/Summary!A1", "values.foo", "values.foo", "values.foo"),
        ("{book}/values:batchGet?ranges=A1", "valueRanges.values.x", None, "valueRanges.values.x"),
        # a path ahead of an unpaired bracket is read first
        ("{book}", "a)", None, "a"),
        ("{book}", "bogus)spreadsheetId", None, "bogus"),
        (
            "{book}",
            "sheets(properties/title,bogus",
            "sheets(properties.title,bogus",
            "sheets.bogus",
        ),
    ],
)
def test_a_fields_mask_naming_no_field_is_refused(gc, gh, book, url, mask, field, path):
    """Measured: the top-level message is generic and the failing PATH is named only inside a
    google.rpc.BadRequest detail, beside the whole mask (`gerr.bad_field_mask`). Matching is
    case-sensitive, and the first path in the mask's order that names nothing is the one named."""
    r = gc.get(
        f"/sheets/v4/spreadsheets/{url.format(book=book)}", headers=gh, params={"fields": mask}
    )
    assert r.status_code == 400
    err = r.json()["error"]
    assert err["message"] == "Request contains an invalid argument."
    assert err["status"] == "INVALID_ARGUMENT"
    assert err["details"] == [
        {
            "@type": "type.googleapis.com/google.rpc.BadRequest",
            "fieldViolations": [
                {
                    "field": field or mask,
                    "description": (
                        "Error expanding 'fields' parameter. Cannot find matching fields for "
                        f"path '{path}'."
                    ),
                }
            ],
        }
    ]


@pytest.mark.parametrize(
    "mask, unpaired",
    [
        ("(", "("),
        ("sheets(", "("),
        ("sheets(properties/title", "("),
        ("sheets(properties(title)", "("),
        ("spreadsheetId,(", "("),
        ("sheets()(", "("),
        (")", ")"),
        ("spreadsheetId)", ")"),
        ("spreadsheetId)bogus", ")"),
        ("sheets(properties))", ")"),
    ],
)
def test_a_fields_mask_with_an_unpaired_bracket_is_refused(gc, gh, book, mask, unpaired):
    """`gerr.unbalanced_field_mask`, reached once the paths before the bracket name something."""
    r = gc.get(f"/sheets/v4/spreadsheets/{book}", headers=gh, params={"fields": mask})
    assert r.status_code == 400
    pair = ")" if unpaired == "(" else "("
    dotted = mask.replace("/", ".")
    assert r.json()["error"]["details"][0]["fieldViolations"] == [
        {
            "field": dotted,
            "description": (
                f"Invalid FieldMask '{dotted}'. Cannot find matching '{pair}' for all '{unpaired}'."
            ),
        }
    ]


_A1_INDENTED = (
    b'{\n  "range": "Summary!A1",\n  "majorDimension": "ROWS",\n  "values": [\n    [\n'
    b'      "Region"\n    ]\n  ]\n}\n'
)
_A1_COMPACT = b'{"range":"Summary!A1","majorDimension":"ROWS","values":[["Region"]]}'


@pytest.mark.parametrize(
    "value, compact",
    [
        (None, False),
        ("false", True),
        ("0", True),
        ("FALSE", False),
        ("False", False),
        ("f", False),
        ("F", False),
        ("no", False),
        ("n", False),
        ("00", False),
        (" false", False),
        ("", False),
        ("NOPE", False),
    ],
)
def test_pretty_print_indents_by_default_and_is_compact_to_the_byte_when_off(
    gc, gh, book, value, compact
):
    """Pins `gerr.respond`'s two renderings to the byte: compact at the spellings
    `_sheets_respond` records, indented with no parameter and at every other value, and a 200
    whatever the value, `NOPE` included."""
    url = f"/sheets/v4/spreadsheets/{book}/values/Summary!A1:A1"
    r = gc.get(url, headers=gh, params={} if value is None else {"prettyPrint": value})
    assert r.status_code == 200
    assert r.content == (_A1_COMPACT if compact else _A1_INDENTED)


@pytest.mark.parametrize(
    "alt, message",
    [
        ("media", 'Unsupported alt type "media" for non byte stream request.'),
        ("NOPE", "Invalid value \"NOPE\" for query parameter 'alt'"),
    ],
)
def test_alt_serves_json_and_refuses_anything_else(gc, gh, book, alt, message):
    """`NOPE` is refused ahead of the credential and `media` behind it (`_sheets_alt`)."""
    url = f"/sheets/v4/spreadsheets/{book}/values/Summary!A1:A1"
    assert gc.get(url, headers=gh, params={"alt": "json"}).status_code == 200
    r = gc.get(url, headers=gh, params={"alt": alt})
    assert r.status_code == 400
    assert r.json()["error"]["message"] == message
    assert gc.get(url, params={"alt": alt}).status_code == (403 if alt == "media" else 400)


def test_callback_wraps_the_body_as_jsonp(gc, gh, book):
    r = gc.get(
        f"/sheets/v4/spreadsheets/{book}/values/Summary!A1:A1",
        headers=gh,
        params={"callback": "cb"},
    )
    assert r.headers["content-type"].startswith("text/javascript")
    assert r.text.startswith("// API callback\ncb({") and r.text.endswith(");")


@pytest.mark.parametrize("param", ["quotaUser", "upload_protocol"])
@pytest.mark.parametrize("value", ["x", ""])
def test_a_param_with_no_effect_here_is_still_accepted(gc, gh, book, param, value):
    """`quotaUser` picks a rate-limit bucket and `upload_protocol` belongs to uploads; neither
    changes a read's answer, and measured, the real API takes both without complaint."""
    r = gc.get(
        f"/sheets/v4/spreadsheets/{book}/values/Summary!A1:A1", headers=gh, params={param: value}
    )
    assert r.status_code == 200


# --- the two reads issued over POST -------------------------------------------------------------


def _by_filter(gc, gh, book, body):
    return gc.post(
        f"/sheets/v4/spreadsheets/{book}/values:batchGetByDataFilter", headers=gh, json=body
    )


def test_batch_get_by_data_filter_answers_each_filter_with_the_filter_beside_it(gc, gh, book):
    """The entry `sheets_values_batch_get_by_data_filter` describes, for one filter."""
    r = _by_filter(gc, gh, book, {"dataFilters": [{"a1Range": "Summary!A1:B2"}]})
    assert r.status_code == 200, r.text
    assert r.json() == {
        "spreadsheetId": book,
        "valueRanges": [
            {
                "valueRange": {
                    "range": "Summary!A1:B2",
                    "majorDimension": "ROWS",
                    "values": [["Region", "Deals"], ["EMEA", "12"]],
                },
                "dataFilters": [{"a1Range": "Summary!A1:B2"}],
            }
        ],
    }


def test_a_data_filter_may_name_a_grid_range_instead_of_an_a1_string(gc, gh, book):
    sheet_id = gc.get(f"/sheets/v4/spreadsheets/{book}", headers=gh).json()["sheets"][0][
        "properties"
    ]["sheetId"]
    grid = {
        "sheetId": sheet_id,
        "startRowIndex": 0,
        "endRowIndex": 2,
        "startColumnIndex": 0,
        "endColumnIndex": 2,
    }
    r = _by_filter(gc, gh, book, {"dataFilters": [{"gridRange": grid}]})
    assert r.status_code == 200
    got = r.json()["valueRanges"][0]
    assert got["valueRange"]["range"] == "Summary!A1:B2"
    # the first sheet's `sheetId` is 0, which the echo leaves out (`_sheets_filter_echo`)
    assert got["dataFilters"] == [{"gridRange": {k: v for k, v in grid.items() if k != "sheetId"}}]


def test_the_answers_come_back_sorted_by_where_each_range_starts(gc, gh, book):
    """NOT the order the filters arrived in. Measured: column before row, so `A2` precedes `B1`,
    and a row number sorts numerically, so `B9` precedes `B10`."""
    r = _by_filter(
        gc,
        gh,
        book,
        {"dataFilters": [{"a1Range": "Summary!B1"}, {"a1Range": "Summary!A2"}]},
    )
    assert [v["valueRange"]["range"] for v in r.json()["valueRanges"]] == [
        "Summary!A2",
        "Summary!B1",
    ]
    r = _by_filter(
        gc,
        gh,
        book,
        {"dataFilters": [{"a1Range": "Summary!B10"}, {"a1Range": "Summary!B9"}]},
    )
    assert [v["valueRange"]["range"] for v in r.json()["valueRanges"]] == [
        "Summary!B9",
        "Summary!B10",
    ]


# Data-filter requests measured against real Sheets on 2026-10-04, and those below the lines
# `# measured 2026-10-05` and `# measured 2026-10-06` on those days, as
# ``(route, target, body, status, shown)``: `values` is `values:batchGetByDataFilter` and `sheet` is
# `:getByDataFilter`; the target is the probe-shaped spreadsheet
# `test_the_data_filter_reads_answer_every_measured_request` builds, one no spreadsheet has
# (`nosuch`), or the probe with no credential (`anon`); the body is the bytes sent, with the probe's
# sheet ids as `ID_SHEET1`, `ID_DATA` and `ID_R1C1`. ``shown`` is the message of a refusal,
# `(message, reason, domain)` for one sent with `$.xgafv=1`, and of a success each answer's
# `(range, majorDimension, echoed filters…)` on `values` (``None`` for no `valueRanges` at all) and
# each sheet's `(title, data blocks)` on `sheet`, a block's `rowData` left out and its other lists
# by their length. The two `sheetId: 0` rows were sent to a spreadsheet whose one sheet has that id,
# as the probe's first sheet has here.
# fmt: off
MEASURED_BY_FILTER = [
    ('values', 'probe', b'{"dataFilters": [{"gridRange": {"sheetId": ID_SHEET1, "startRowIndex": "abc"}}]}', 400, 'Invalid value at \'data_filters[0].grid_range.start_row_index.value\' (TYPE_INT32), "abc"'),
    ('values', 'probe', b'{"dataFilters": [{"gridRange": {"sheetId": ID_SHEET1, "startRowIndex": 1.7}}]}', 400, "Invalid value at 'data_filters[0].grid_range.start_row_index.value' (TYPE_INT32), 1.7"),
    ('values', 'probe', b'{"dataFilters": [{"gridRange": {"sheetId": ID_SHEET1, "startRowIndex": true}}]}', 400, "Invalid value at 'data_filters[0].grid_range.start_row_index.value' (TYPE_INT32), true"),
    ('values', 'probe', b'{"dataFilters": [{"gridRange": {"sheetId": ID_SHEET1, "startRowIndex": []}}]}', 400, 'Invalid JSON payload received. Unknown name "startRowIndex" at \'data_filters[0].grid_range\': Proto field is not repeating, cannot start list.'),
    ('values', 'probe', b'{"dataFilters": [{"gridRange": {"sheetId": ID_SHEET1, "startRowIndex": {"a": 1}}}]}', 400, 'Invalid JSON payload received. Unknown name "a" at \'data_filters[0].grid_range.start_row_index\': Cannot find field.'),
    ('values', 'probe', b'{"dataFilters": [{"gridRange": {"sheetId": ID_SHEET1, "startRowIndex": ""}}]}', 400, 'Invalid value at \'data_filters[0].grid_range.start_row_index.value\' (TYPE_INT32), ""'),
    ('values', 'probe', b'{"dataFilters": [{"gridRange": {"sheetId": ID_SHEET1, "startRowIndex": " 1"}}]}', 400, 'Invalid value at \'data_filters[0].grid_range.start_row_index.value\' (TYPE_INT32), " 1"'),
    ('values', 'probe', b'{"dataFilters": [{"gridRange": {"sheetId": ID_SHEET1, "startRowIndex": "1.0"}}]}', 400, 'Invalid value at \'data_filters[0].grid_range.start_row_index.value\' (TYPE_INT32), "1.0"'),
    ('values', 'probe', b'{"dataFilters": [{"gridRange": {"sheetId": ID_SHEET1, "startRowIndex": "0x1"}}]}', 400, 'Invalid value at \'data_filters[0].grid_range.start_row_index.value\' (TYPE_INT32), "0x1"'),
    ('values', 'probe', b'{"dataFilters": [{"gridRange": {"sheetId": ID_SHEET1, "startRowIndex": 2147483648}}]}', 400, "Invalid value at 'data_filters[0].grid_range.start_row_index.value' (TYPE_INT32), 2147483648"),
    ('values', 'probe', b'{"dataFilters": [{"gridRange": {"sheetId": ID_SHEET1, "startRowIndex": "2147483648"}}]}', 400, 'Invalid value at \'data_filters[0].grid_range.start_row_index.value\' (TYPE_INT32), "2147483648"'),
    ('values', 'probe', b'{"dataFilters": [{"gridRange": {"sheetId": ID_SHEET1, "startRowIndex": 1e+20}}]}', 400, "Invalid value at 'data_filters[0].grid_range.start_row_index.value' (TYPE_INT32), 1e+20"),
    ('values', 'probe', b'{"dataFilters": [{"gridRange": {"sheetId": ID_SHEET1, "startRowIndex": -0.5}}]}', 400, "Invalid value at 'data_filters[0].grid_range.start_row_index.value' (TYPE_INT32), -0.5"),
    ('values', 'probe', b'{"dataFilters": [{"gridRange": {"sheetId": ID_SHEET1, "startRowIndex": 2147483647}}]}', 400, 'Invalid dataFilter[0]: Range (Sheet1!-2147483648:) exceeds grid limits. Max rows: 1000, max columns: 26'),
    ('sheet', 'probe', b'{"dataFilters": [{"gridRange": {"sheetId": ID_SHEET1, "startRowIndex": "abc"}}]}', 400, 'Invalid value at \'data_filters[0].grid_range.start_row_index.value\' (TYPE_INT32), "abc"'),
    ('values', 'probe', b'{"dataFilters": [{"gridRange": {"sheetId": "abc"}}]}', 400, 'Invalid value at \'data_filters[0].grid_range.sheet_id\' (TYPE_INT32), "abc"'),
    ('values', 'probe', b'{"dataFilters": [{"gridRange": {"sheetId": {}}}]}', 400, "Invalid value at 'data_filters[0].grid_range' (sheet_id), Starting an object on a scalar field"),
    ('values', 'probe', b'{"dataFilters": [{"gridRange": {"sheetId": []}}]}', 400, 'Invalid JSON payload received. Unknown name "sheetId" at \'data_filters[0].grid_range\': Proto field is not repeating, cannot start list.'),
    ('values', 'probe', b'{"dataFilters": [{"gridRange": {"sheetId": -1}}]}', 400, 'Invalid dataFilter[0]: No grid with id: -1'),
    ('values', 'probe', b'{"dataFilters": [{"gridRange": {"sheetId": ID_SHEET1, "startRowIndex": {"value": "abc"}}}]}', 400, 'Invalid value at \'data_filters[0].grid_range.start_row_index.value\' (TYPE_INT32), "abc"'),
    ('values', 'probe', b'{"dataFilters": [{"a1Range": 5}]}', 400, "Invalid value at 'data_filters[0].a1_range' (TYPE_STRING), 5"),
    ('values', 'probe', b'{"dataFilters": [{"a1Range": null}]}', 400, 'Invalid dataFilter[0]: dataFilter.filter must be specified.'),
    ('values', 'probe', b'{"dataFilters": [{"a1Range": []}]}', 400, 'Invalid JSON payload received. Unknown name "a1Range" at \'data_filters[0]\': Proto field is not repeating, cannot start list.'),
    ('values', 'probe', b'{"dataFilters": [{"a1Range": {}}]}', 400, "Invalid value at 'data_filters[0]' (a1_range), Starting an object on a scalar field"),
    ('values', 'probe', b'{"dataFilters": [{"a1Range": ""}]}', 400, 'Invalid dataFilter[0]: Unable to parse range: '),
    ('values', 'probe', b'{"dataFilters": [{"gridRange": "abc"}]}', 400, 'Invalid value at \'data_filters[0].grid_range\' (type.googleapis.com/google.apps.sheets.v4.GridRange), "abc"'),
    ('values', 'probe', b'{"dataFilters": [{"gridRange": []}]}', 400, 'Invalid JSON payload received. Unknown name "gridRange" at \'data_filters[0]\': Proto field is not repeating, cannot start list.'),
    ('values', 'probe', b'{"dataFilters": "abc"}', 400, 'Invalid value at \'data_filters\' (type.googleapis.com/google.apps.sheets.v4.DataFilter), "abc"'),
    ('values', 'probe', b'{"dataFilters": {}}', 400, 'Invalid dataFilter[0]: dataFilter.filter must be specified.'),
    ('values', 'probe', b'{"dataFilters": ["abc"]}', 400, 'Invalid value at \'data_filters[0]\' (type.googleapis.com/google.apps.sheets.v4.DataFilter), "abc"'),
    ('values', 'probe', b'{"dataFilters": [null]}', 400, 'Must specify at least one dataFilter.'),
    ('values', 'probe', b'{"dataFilters": [[]]}', 400, 'Must specify at least one dataFilter.'),
    ('values', 'probe', b'{"dataFilters": [{}]}', 400, 'Invalid dataFilter[0]: dataFilter.filter must be specified.'),
    ('values', 'probe', b'{"dataFilters": [{"a1Range": "Sheet1!A1", "gridRange": {"sheetId": ID_SHEET1}}]}', 400, "Invalid value at 'data_filters[0]' (oneof), oneof field 'filter' is already set. Cannot set 'gridRange'"),
    ('values', 'probe', b'{"dataFilters": [{"a1Range": "Sheet1!A1"}], "bogus": 1}', 400, 'Invalid JSON payload received. Unknown name "bogus": Cannot find field.'),
    ('values', 'probe', b'{"dataFilters": [{"a1Range": "Sheet1!A1", "bogus": 1}]}', 400, 'Invalid JSON payload received. Unknown name "bogus" at \'data_filters[0]\': Cannot find field.'),
    ('values', 'probe', b'{"dataFilters": [{"gridRange": {"sheetId": ID_SHEET1, "bogus": 1}}]}', 400, 'Invalid JSON payload received. Unknown name "bogus" at \'data_filters[0].grid_range\': Cannot find field.'),
    ('values', 'probe', b'{"dataFilters": [{"a1Range": "Sheet1!A1"}], "bogus": 1, "majorDimension": "NOPE"}', 400, 'Invalid JSON payload received. Unknown name "bogus": Cannot find field.\nInvalid value at \'major_dimension\' (type.googleapis.com/google.apps.sheets.v4.Dimension), "NOPE"'),
    ('values', 'probe', b'{"majorDimension": "NOPE", "dataFilters": [{"a1Range": "Sheet1!A1"}], "bogus": 1}', 400, 'Invalid value at \'major_dimension\' (type.googleapis.com/google.apps.sheets.v4.Dimension), "NOPE"\nInvalid JSON payload received. Unknown name "bogus": Cannot find field.'),
    ('values', 'probe', b'{"dataFilters": [{"a1Range": "Sheet1!A1"}], "includeGridData": true}', 400, 'Invalid JSON payload received. Unknown name "includeGridData": Cannot find field.'),
    ('values', 'probe', b'[]', 400, 'Invalid JSON payload received. Unknown name "": Root element must be a message.'),
    ('values', 'probe', b'5', 400, 'Invalid JSON payload received. Unknown name "": Root element must be a message.'),
    ('values', 'probe', b'', 400, 'Must specify at least one dataFilter.'),
    ('values', 'probe', b'{"dataFilters": [{"gridRange": {"sheetId": ID_SHEET1, "startRowIndex": "abc"}}]}', 400, ('Invalid value at \'data_filters[0].grid_range.start_row_index.value\' (TYPE_INT32), "abc"', 'invalid', None)),
    ('values', 'probe', b'{"dataFilters": [{"a1Range": "Sheet1!A1"}], "bogus": 1}', 400, ('Invalid JSON payload received. Unknown name "bogus": Cannot find field.', 'invalid', None)),
    ('values', 'probe', b'[]', 400, ('Invalid JSON payload received. Unknown name "": Root element must be a message.', 'invalid', None)),
    ('values', 'probe', b'{"dataFilters": [{"gridRange": {"sheetId": ID_SHEET1, "startRowIndex": -1}}]}', 400, ('Invalid dataFilter[0]: GridRange indexes must be >= 0', 'badRequest', 'global')),
    ('values', 'probe', b'{"dataFilters": [{"gridRange": {"sheetId": 5}}]}', 400, ('Invalid dataFilter[0]: No grid with id: 5', 'badRequest', 'global')),
    ('values', 'probe', b'{}', 400, ('Must specify at least one dataFilter.', 'badRequest', 'global')),
    ('values', 'probe', b'{"DataFilters": [{"a1Range": "Sheet1!A1"}]}', 400, 'Invalid JSON payload received. Unknown name "DataFilters": Cannot find field.'),
    ('values', 'nosuch', b'{"dataFilters": [{"gridRange": {"sheetId": ID_SHEET1, "startRowIndex": "abc"}}]}', 400, 'Invalid value at \'data_filters[0].grid_range.start_row_index.value\' (TYPE_INT32), "abc"'),
    ('values', 'nosuch', b'{"dataFilters": [{"a1Range": "Sheet1!A1"}], "bogus": 1}', 400, 'Invalid JSON payload received. Unknown name "bogus": Cannot find field.'),
    ('values', 'nosuch', b'[]', 400, 'Invalid JSON payload received. Unknown name "": Root element must be a message.'),
    ('values', 'nosuch', b'{"dataFilters": [{"a1Range": "Sheet1!A1", "gridRange": {"sheetId": ID_SHEET1}}]}', 400, "Invalid value at 'data_filters[0]' (oneof), oneof field 'filter' is already set. Cannot set 'gridRange'"),
    ('values', 'nosuch', b'{}', 404, 'Requested entity was not found.'),
    ('values', 'nosuch', b'{"dataFilters": [{}]}', 404, 'Requested entity was not found.'),
    ('values', 'nosuch', b'{"dataFilters": [{"gridRange": {"sheetId": ID_SHEET1, "startRowIndex": -1}}]}', 404, 'Requested entity was not found.'),
    ('values', 'nosuch', b'{"dataFilters": [{"gridRange": {"sheetId": ID_SHEET1, "startRowIndex": 2, "endRowIndex": 1}}]}', 404, 'Requested entity was not found.'),
    ('values', 'nosuch', b'{"dataFilters": [{"gridRange": {"sheetId": 5}}]}', 404, 'Requested entity was not found.'),
    ('values', 'nosuch', b'{"dataFilters": [{"a1Range": "Nope!A1"}]}', 404, 'Requested entity was not found.'),
    ('sheet', 'nosuch', b'{"dataFilters": [{}]}', 404, 'Requested entity was not found.'),
    ('values', 'anon', b'{"dataFilters": [{"gridRange": {"sheetId": ID_SHEET1, "startRowIndex": "abc"}}]}', 401, 'Request is missing required authentication credential. Expected OAuth 2 access token, login cookie or other valid authentication credential. See https://developers.google.com/identity/sign-in/web/devconsole-project.'),
    ('values', 'anon', b'abc', 401, 'Request is missing required authentication credential. Expected OAuth 2 access token, login cookie or other valid authentication credential. See https://developers.google.com/identity/sign-in/web/devconsole-project.'),
    ('sheet', 'anon', b'{"dataFilters": [{"gridRange": {"sheetId": ID_SHEET1, "startRowIndex": "abc"}}]}', 401, 'Request is missing required authentication credential. Expected OAuth 2 access token, login cookie or other valid authentication credential. See https://developers.google.com/identity/sign-in/web/devconsole-project.'),
    ('values', 'probe', b'{"dataFilters": [{"gridRange": {"sheetId": ID_SHEET1, "startRowIndex": -1}}]}', 400, 'Invalid dataFilter[0]: GridRange indexes must be >= 0'),
    ('values', 'probe', b'{"dataFilters": [{"gridRange": {"sheetId": ID_SHEET1, "endColumnIndex": -1}}]}', 400, 'Invalid dataFilter[0]: GridRange indexes must be >= 0'),
    ('values', 'probe', b'{"dataFilters": [{"gridRange": {"sheetId": ID_SHEET1, "startRowIndex": 2, "endRowIndex": 1}}]}', 400, 'Invalid dataFilter[0]: endRowIndex[1] cannot be before startRowIndex[2]'),
    ('values', 'probe', b'{"dataFilters": [{"gridRange": {"sheetId": ID_SHEET1, "startColumnIndex": 2, "endColumnIndex": 1}}]}', 400, 'Invalid dataFilter[0]: endColumnIndex[1] cannot be before startColumnIndex[2]'),
    ('values', 'probe', b'{"dataFilters": [{"gridRange": {"sheetId": ID_SHEET1, "startColumnIndex": 2, "endColumnIndex": 1, "startRowIndex": 2, "endRowIndex": 1}}]}', 400, 'Invalid dataFilter[0]: endRowIndex[1] cannot be before startRowIndex[2]'),
    ('values', 'probe', b'{"dataFilters": [{"gridRange": {"sheetId": ID_SHEET1, "startRowIndex": 2, "endRowIndex": 1, "startColumnIndex": -1}}]}', 400, 'Invalid dataFilter[0]: GridRange indexes must be >= 0'),
    ('values', 'probe', b'{"dataFilters": [{"gridRange": {"sheetId": ID_SHEET1, "startRowIndex": 1000, "endRowIndex": 1000}}]}', 400, 'Invalid dataFilter[0]: Range ((empty) Sheet1!1001:1000) exceeds grid limits. Max rows: 1000, max columns: 26'),
    ('values', 'probe', b'{"dataFilters": [{"gridRange": {"sheetId": ID_SHEET1, "startRowIndex": 1000}}]}', 400, 'Invalid dataFilter[0]: Range (Sheet1!1001:) exceeds grid limits. Max rows: 1000, max columns: 26'),
    ('values', 'probe', b'{"dataFilters": [{"gridRange": {"sheetId": ID_SHEET1, "startColumnIndex": 26}}]}', 400, 'Invalid dataFilter[0]: Range (Sheet1!AA:) exceeds grid limits. Max rows: 1000, max columns: 26'),
    ('values', 'probe', b'{"dataFilters": [{"gridRange": {"sheetId": ID_SHEET1, "startRowIndex": 0, "endRowIndex": 0, "startColumnIndex": 2, "endColumnIndex": 1}}]}', 400, 'Invalid dataFilter[0]: endColumnIndex[1] cannot be before startColumnIndex[2]'),
    ('values', 'probe', b'{"dataFilters": [{"gridRange": {"sheetId": 5}}]}', 400, 'Invalid dataFilter[0]: No grid with id: 5'),
    ('values', 'probe', b'{"dataFilters": [{"gridRange": {"sheetId": ID_SHEET1, "startRowIndex": -1}}, {"gridRange": {"sheetId": ID_SHEET1, "startRowIndex": 2, "endRowIndex": 1}}]}', 400, 'Invalid dataFilter[0]: GridRange indexes must be >= 0'),
    ('values', 'probe', b'{"dataFilters": [{"gridRange": {"sheetId": ID_SHEET1, "startRowIndex": 2, "endRowIndex": 1}}, {"gridRange": {"sheetId": ID_SHEET1, "startRowIndex": -1}}]}', 400, 'Invalid dataFilter[0]: endRowIndex[1] cannot be before startRowIndex[2]'),
    ('values', 'probe', b'{"dataFilters": [{"a1Range": "Nope!A1"}, {"gridRange": {"sheetId": ID_SHEET1, "startRowIndex": -1}}]}', 400, 'Invalid dataFilter[0]: Unable to parse range: Nope!A1'),
    ('values', 'probe', b'{"dataFilters": [{"gridRange": {"sheetId": ID_SHEET1, "startRowIndex": -1}}, {"a1Range": "Nope!A1"}]}', 400, 'Invalid dataFilter[0]: GridRange indexes must be >= 0'),
    ('sheet', 'probe', b'{"dataFilters": [{"gridRange": {"sheetId": ID_SHEET1, "startRowIndex": -1}}]}', 400, 'GridRange indexes must be >= 0'),
    ('sheet', 'probe', b'{"dataFilters": [{"gridRange": {"sheetId": ID_SHEET1, "startRowIndex": 2, "endRowIndex": 1}}]}', 400, 'endRowIndex[1] cannot be before startRowIndex[2]'),
    ('sheet', 'probe', b'{"dataFilters": [{"gridRange": {"sheetId": ID_SHEET1, "startRowIndex": 1000}}]}', 400, 'Range (Sheet1!1001:) exceeds grid limits. Max rows: 1000, max columns: 26'),
    ('sheet', 'probe', b'{"dataFilters": [{"gridRange": {"sheetId": 5}}]}', 400, 'No sheet with id: 5'),
    ('values', 'probe', b'{"dataFilters": [{"gridRange": {"sheetId": ID_SHEET1, "startRowIndex": 1000, "startColumnIndex": 1, "endColumnIndex": 3}}]}', 400, 'Invalid dataFilter[0]: Range (Sheet1!B1001:C) exceeds grid limits. Max rows: 1000, max columns: 26'),
    ('values', 'probe', b'{"dataFilters": [{"gridRange": {"sheetId": ID_SHEET1, "startRowIndex": 1000, "endRowIndex": 1005, "endColumnIndex": 3}}]}', 400, 'Invalid dataFilter[0]: Range (Sheet1!1001:C1005) exceeds grid limits. Max rows: 1000, max columns: 26'),
    ('values', 'probe', b'{"dataFilters": [{"gridRange": {"sheetId": ID_SHEET1, "startRowIndex": 1000, "endRowIndex": 1001, "startColumnIndex": 0, "endColumnIndex": 1}}]}', 400, 'Invalid dataFilter[0]: Range (Sheet1!A1001) exceeds grid limits. Max rows: 1000, max columns: 26'),
    ('values', 'probe', b'{"dataFilters": [{"gridRange": {"sheetId": ID_SHEET1, "startColumnIndex": 26, "endRowIndex": 5}}]}', 400, 'Invalid dataFilter[0]: Range (Sheet1!AA:5) exceeds grid limits. Max rows: 1000, max columns: 26'),
    ('values', 'probe', b'{"dataFilters": [{"gridRange": {"sheetId": ID_SHEET1, "startColumnIndex": 26, "endColumnIndex": 26}}]}', 400, 'Invalid dataFilter[0]: Range ((empty) Sheet1!AA:Z) exceeds grid limits. Max rows: 1000, max columns: 26'),
    ('values', 'probe', b'{"dataFilters": [{"gridRange": {"sheetId": ID_SHEET1, "startRowIndex": 1000, "endRowIndex": 1000, "startColumnIndex": 1, "endColumnIndex": 3}}]}', 400, 'Invalid dataFilter[0]: Range ((empty) Sheet1!B1001:C1000) exceeds grid limits. Max rows: 1000, max columns: 26'),
    ('values', 'probe', b'{"dataFilters": [{"gridRange": {"sheetId": ID_SHEET1, "startRowIndex": 2000, "endRowIndex": 1500}}]}', 400, 'Invalid dataFilter[0]: endRowIndex[1500] cannot be before startRowIndex[2000]'),
    ('values', 'probe', b'{"dataFilters": [{"gridRange": {"sheetId": ID_SHEET1, "startRowIndex": 5, "endRowIndex": 5, "startColumnIndex": 26}}]}', 400, 'Invalid dataFilter[0]: Range ((empty) Sheet1!AA6:5) exceeds grid limits. Max rows: 1000, max columns: 26'),
    ('values', 'probe', b'{"dataFilters": [{"gridRange": {"sheetId": ID_R1C1, "startRowIndex": 1000}}]}', 400, "Invalid dataFilter[0]: Range ('R1C1'!1001:) exceeds grid limits. Max rows: 1000, max columns: 26"),
    ('values', 'probe', b'\xef\xbb\xbf{}', 400, 'Must specify at least one dataFilter.'),
    ('sheet', 'probe', b'{"dataFilters": [{"a1Range": "Sheet1!A1"}], "includeGridData": 2}', 400, "Invalid value at 'include_grid_data' (TYPE_BOOL), 2"),
    ('values', 'probe', b'{"dataFilters": [{"a1Range": "Sheet1!A1:B2"}], "majorDimension": 3}', 500, 'Internal error encountered.'),
    ('values', 'probe', b'{"dataFilters": [{"a1Range": "Sheet1!A1:B2"}], "majorDimension": 1.5}', 400, "Invalid value at 'major_dimension' (type.googleapis.com/google.apps.sheets.v4.Dimension), 1.5"),
    ('values', 'probe', b'{"dataFilters": [{"a1Range": "Sheet1!A1:B2"}], "majorDimension": true}', 400, "Invalid value at 'major_dimension' (type.googleapis.com/google.apps.sheets.v4.Dimension), true"),
    ('values', 'probe', b'{"dataFilters": [{"a1Range": "Sheet1!A1:B2"}], "majorDimension": "3"}', 400, 'Invalid value at \'major_dimension\' (type.googleapis.com/google.apps.sheets.v4.Dimension), "3"'),
    ('values', 'probe', b'{"dataFilters": [{"a1Range": "Sheet1!A1:B2"}], "majorDimension": " 2"}', 400, 'Invalid value at \'major_dimension\' (type.googleapis.com/google.apps.sheets.v4.Dimension), " 2"'),
    ('values', 'probe', b'{"dataFilters": [{"a1Range": "Sheet1!A1:B2"}], "majorDimension": "dimensionUnspecified"}', 400, 'Invalid value at \'major_dimension\' (type.googleapis.com/google.apps.sheets.v4.Dimension), "dimensionUnspecified"'),
    ('values', 'probe', b'{"dataFilters": [{"a1Range": "Sheet1!A1:B2"}], "majorDimension": {}}', 400, 'Invalid value (major_dimension), Starting an object on a scalar field'),
    ('values', 'probe', b'{"dataFilters": [{"a1Range": "Sheet1!A1:B2"}], "majorDimension": []}', 400, 'Invalid JSON payload received. Unknown name "majorDimension": Proto field is not repeating, cannot start list.'),
    ('values', 'probe', b'{"dataFilters": [{"a1Range": "Sheet1!A1"}], "valueRenderOption": 3}', 400, 'Invalid valueRenderOption: UNRECOGNIZED'),
    ('values', 'probe', b'{"dataFilters": [{"developerMetadataLookup": {}}]}', 200, None),
    ('values', 'probe', b'{"dataFilters": [{"developerMetadataLookup": {"bogus": 1}}]}', 400, 'Invalid JSON payload received. Unknown name "bogus" at \'data_filters[0].developer_metadata_lookup\': Cannot find field.'),
    ('values', 'probe', b'{"dataFilters": [{"developerMetadataLookup": {"locationType": "NOPE"}}]}', 400, 'Invalid value at \'data_filters[0].developer_metadata_lookup.location_type\' (type.googleapis.com/google.apps.sheets.v4.DeveloperMetadataLocationType), "NOPE"'),
    ('values', 'probe', b'{"dataFilters": [{"developerMetadataLookup": "abc"}]}', 400, 'Invalid value at \'data_filters[0].developer_metadata_lookup\' (type.googleapis.com/google.apps.sheets.v4.DeveloperMetadataLookup), "abc"'),
    ('values', 'probe', b'{"dataFilters": [{"gridRange": {"sheetId": ID_SHEET1, "startRowIndex": "a\\"b"}}]}', 400, 'Invalid value at \'data_filters[0].grid_range.start_row_index.value\' (TYPE_INT32), "a"b"'),
    ('values', 'probe', b'{"dataFilters":[{"gridRange":{"sheetId":ID_SHEET1,"startRowIndex":1E20}}]}', 400, "Invalid value at 'data_filters[0].grid_range.start_row_index.value' (TYPE_INT32), 1e+20"),
    ('values', 'probe', b'{"dataFilters":[{"gridRange":{"sheetId":ID_SHEET1,"startRowIndex":12345678901234567890}}]}', 400, "Invalid value at 'data_filters[0].grid_range.start_row_index.value' (TYPE_INT32), 12345678901234567890"),
    ('values', 'probe', b'{"dataFilters":[{"gridRange":{"sheetId":ID_SHEET1,"startRowIndex":1e-2}}]}', 400, "Invalid value at 'data_filters[0].grid_range.start_row_index.value' (TYPE_INT32), 0.01"),
    ('values', 'probe', b'{"dataFilters": [{"a1Range": "Sheet1!A1"}], "excludeTablesInBandedRanges": true}', 400, 'Invalid JSON payload received. Unknown name "excludeTablesInBandedRanges": Cannot find field.'),
    ('values', 'probe', b'{"dataFilters": [{"developerMetadataLookup": {"metadataId": "abc"}}]}', 400, 'Invalid value at \'data_filters[0].developer_metadata_lookup.metadata_id.value\' (TYPE_INT32), "abc"'),
    ('values', 'probe', b'{"dataFilters": [{"developerMetadataLookup": {"metadataLocation": {"sheetId": {}}}}]}', 400, "Invalid value at 'data_filters[0].developer_metadata_lookup.metadata_location' (sheet_id), Starting an object on a scalar field"),
    ('values', 'probe', b'{"dataFilters": [{"developerMetadataLookup": {"metadataLocation": {"spreadsheet": "maybe"}}}]}', 400, 'Invalid value at \'data_filters[0].developer_metadata_lookup.metadata_location.spreadsheet\' (TYPE_BOOL), "maybe"'),
    ('values', 'probe', b'{"dataFilters": [{"developerMetadataLookup": {"metadataKey": 5}}]}', 400, "Invalid value at 'data_filters[0].developer_metadata_lookup.metadata_key.value' (TYPE_STRING), 5"),
    ('values', 'probe', b'{"dataFilters": [{"developerMetadataLookup": {"metadataLocation": {"spreadsheet": true, "sheetId": 1}}}]}', 400, "Invalid value at 'data_filters[0].developer_metadata_lookup.metadata_location' (oneof), oneof field 'location' is already set. Cannot set 'sheetId'"),
    ('values', 'nosuch', b'{"dataFilters": [{"a1Range": "Sheet1!A1"}], "majorDimension": 3}', 404, 'Requested entity was not found.'),
    ('values', 'nosuch', b'{"dataFilters": [{"a1Range": "Sheet1!A1"}], "valueRenderOption": 3}', 404, 'Requested entity was not found.'),
    ('values', 'probe', b'{"dataFilters": [{"a1Range": "Sheet1!A1"}], "majorDimension": 3, "valueRenderOption": 3}', 400, 'Invalid valueRenderOption: UNRECOGNIZED'),
    ('values', 'probe', b'{"dataFilters": [{"a1Range": "Nope!A1"}], "valueRenderOption": 3}', 400, 'Invalid valueRenderOption: UNRECOGNIZED'),
    ('values', 'probe', b'{"dataFilters": [{"a1Range": "Nope!A1"}], "majorDimension": 3}', 400, 'Invalid dataFilter[0]: Unable to parse range: Nope!A1'),
    ('sheet', 'probe', b'{"dataFilters": [{"a1Range": "Sheet1!A1"}], "commentsViewMode": "NOPE"}', 400, 'Invalid value at \'comments_view_mode\' (type.googleapis.com/google.apps.sheets.v4.CommentsViewMode), "NOPE"'),
    ('values', 'probe', b'{"dataFilters": [{"a1Range": "Sheet1!A1"}], "majorDimension": 3}', 500, ('Internal error encountered.', 'backendError', 'global')),
    ('values', 'probe', b'{"dataFilters": [{"a1Range": "Sheet1!A1"}], "valueRenderOption": 3}', 400, ('Invalid valueRenderOption: UNRECOGNIZED', 'badRequest', 'global')),
    ('values', 'probe', b'{"dataFilters": [{"developerMetadataLookup": {"metadataLocation": {"dimensionRange": {"sheetId": ID_SHEET1, "startIndex": 0, "endIndex": 1}}}}]}', 400, 'Invalid dataFilter[0]: No dimension specified'),
    ('values', 'probe', b'{"dataFilters": [{"developerMetadataLookup": {"metadataLocation": {"dimensionRange": {"sheetId": 5, "dimension": "ROWS", "startIndex": 0, "endIndex": 1}}}}]}', 400, 'Invalid dataFilter[0]: No grid with id: 5'),
    ('values', 'probe', b'{"dataFilters": [{"developerMetadataLookup": {"metadataLocation": {"dimensionRange": {"sheetId": ID_SHEET1, "dimension": "ROWS", "startIndex": -1, "endIndex": 1}}}}]}', 400, 'Invalid dataFilter[0]: DimensionRange must represent a single row or column.'),
    ('values', 'probe', b'{"dataFilters": [{"developerMetadataLookup": {"metadataLocation": {"dimensionRange": {"sheetId": ID_SHEET1, "dimension": "ROWS", "endIndex": 1}}}}]}', 400, 'Invalid dataFilter[0]: DimensionRange must specify both a startIndex and an endIndex.'),
    ('values', 'probe', b'{"dataFilters": [{"developerMetadataLookup": {"metadataLocation": {"sheetId": 5}}}]}', 400, 'Invalid dataFilter[0]: No grid with id: 5'),
    ('values', 'probe', b'{"dataFilters": [{"developerMetadataLookup": {"metadataLocation": {"spreadsheet": true}}}]}', 200, None),
    ('values', 'probe', b'{"dataFilters": [{"developerMetadataLookup": {"locationMatchingStrategy": "INTERSECTING_LOCATION"}}]}', 400, 'Invalid dataFilter[0]: A locationMatchingStrategy was specified, but no metadataLocation was specified: lookups must always specify a metadataLocation when specifying a locationMatchingStrategy.'),
    ('values', 'nosuch', b'{"dataFilters": [{"developerMetadataLookup": {"metadataLocation": {"dimensionRange": {"sheetId": ID_SHEET1, "startIndex": 0}}}}]}', 404, 'Requested entity was not found.'),
    ('sheet', 'probe', b'{"dataFilters": [{"developerMetadataLookup": {"metadataLocation": {"dimensionRange": {"sheetId": ID_SHEET1, "startIndex": 0}}}}]}', 400, 'DimensionRange must specify both a startIndex and an endIndex.'),
    ('values', 'probe', b'{"dataFilters": [{"gridRange": {"sheetId": 0}}]}', 200, [('Sheet1!A1:Z1000', 'ROWS', {'gridRange': {}})]),
    ('values', 'probe', b'{"dataFilters": [{"gridRange": {"sheetId": 0, "startRowIndex": 0}}]}', 200, [('Sheet1!A1:Z1000', 'ROWS', {'gridRange': {'startRowIndex': 0}})]),
    ('values', 'probe', b'{"dataFilters": {"a1Range": 5}}', 400, "Invalid value at 'data_filters.a1_range' (TYPE_STRING), 5"),
    ('values', 'probe', b'{"dataFilters":[{"gridRange":{"sheetId":ID_SHEET1},"gridRange":{"sheetId":ID_SHEET1}}]}', 400, "Invalid value at 'data_filters[0]' (oneof), oneof field 'filter' is already set. Cannot set 'gridRange'"),
    ('values', 'probe', b'{"dataFilters": [{"developerMetadataLookup": {"metadataLocation": {"dimensionRange": {"sheetId": ID_SHEET1, "dimension": "ROWS", "startIndex": 999, "endIndex": 1000}}}}]}', 200, None),
    ('values', 'probe', b'{"dataFilters": [{"developerMetadataLookup": {"metadataLocation": {"dimensionRange": {"sheetId": ID_SHEET1, "dimension": "COLUMNS", "startIndex": 0, "endIndex": 27}}}}]}', 400, 'Invalid dataFilter[0]: DimensionRange must represent a single row or column.'),
    ('values', 'probe', b'{"dataFilters": [{"developerMetadataLookup": {"metadataLocation": {"dimensionRange": {"sheetId": ID_SHEET1, "dimension": "DIMENSION_UNSPECIFIED", "startIndex": 0, "endIndex": 1}}}}]}', 400, 'Invalid dataFilter[0]: No dimension specified'),
    ('values', 'probe', b'{"dataFilters": [{"developerMetadataLookup": {"metadataLocation": {"dimensionRange": {"sheetId": ID_SHEET1, "dimension": "ROWS", "startIndex": 1000, "endIndex": 1001}}}}]}', 400, 'Invalid dataFilter[0]: DimensionRange startIndex [1000] is after the last ROWS index [999] of the sheet [ID_SHEET1].'),
    ('values', 'probe', b'{"dataFilters": [{"gridRange": {"sheetId": ID_SHEET1, "startRowIndex": -1}}, {"developerMetadataLookup": {"metadataLocation": {"sheetId": 5}}}]}', 400, 'Invalid dataFilter[0]: GridRange indexes must be >= 0'),
    ('values', 'probe', b'{"dataFilters": [{"developerMetadataLookup": {"metadataLocation": {"sheetId": 5}}}, {"gridRange": {"sheetId": ID_SHEET1, "startRowIndex": -1}}]}', 400, 'Invalid dataFilter[0]: No grid with id: 5'),
    ('sheet', 'probe', b'{"dataFilters": [{"developerMetadataLookup": {"metadataLocation": {"sheetId": 5}}}]}', 400, 'No grid with id: 5'),
    ('sheet', 'probe', b'{"dataFilters": [{"developerMetadataLookup": {"metadataKey": "k"}}, {"a1Range": "Nope!A1"}]}', 400, 'Unable to parse range: Nope!A1'),
    ('values', 'probe', b'{"dataFilters": [{"developerMetadataLookup": {"metadataLocation": {"dimensionRange": {"sheetId": ID_SHEET1, "dimension": "ROWS", "startIndex": 1000, "endIndex": 1002}}}}]}', 400, 'Invalid dataFilter[0]: DimensionRange must represent a single row or column.'),
    ('values', 'probe', b'{"dataFilters": [{"developerMetadataLookup": {"metadataLocation": {"dimensionRange": {"sheetId": 5, "startIndex": 0, "endIndex": 1}}}}]}', 400, 'Invalid dataFilter[0]: No grid with id: 5'),
    ('values', 'probe', b'{"dataFilters": [{"developerMetadataLookup": {"metadataLocation": {"dimensionRange": {"sheetId": 5, "dimension": "ROWS", "startIndex": 0}}}}]}', 400, 'Invalid dataFilter[0]: DimensionRange must specify both a startIndex and an endIndex.'),
    ('values', 'probe', b'{"dataFilters": [{"developerMetadataLookup": {"metadataLocation": {"dimensionRange": {"sheetId": ID_SHEET1, "startIndex": 0, "endIndex": 5}}}}]}', 400, 'Invalid dataFilter[0]: DimensionRange must represent a single row or column.'),
    ('values', 'probe', b'{"dataFilters": [{"developerMetadataLookup": {"metadataLocation": {"dimensionRange": {"sheetId": ID_SHEET1, "dimension": "COLUMNS", "startIndex": 26, "endIndex": 27}}}}]}', 400, 'Invalid dataFilter[0]: DimensionRange startIndex [26] is after the last COLUMNS index [25] of the sheet [ID_SHEET1].'),
    ('values', 'probe', b'{"dataFilters": [{"developerMetadataLookup": {"metadataLocation": {"dimensionRange": {"sheetId": ID_SHEET1, "dimension": "ROWS", "startIndex": -5, "endIndex": -4}}}}]}', 400, 'Invalid dataFilter[0]: DimensionRange indexes must be >= 0'),
    ('values', 'probe', b'{"dataFilters": [{"developerMetadataLookup": {"locationMatchingStrategy": "EXACT_LOCATION", "metadataKey": "x"}}]}', 400, 'Invalid dataFilter[0]: A locationMatchingStrategy was specified, but no metadataLocation was specified: lookups must always specify a metadataLocation when specifying a locationMatchingStrategy.'),
    ('values', 'probe', b'{"dataFilters": [{"gridRange": {"sheetId": ID_SHEET1, "startRowIndex": 1000, "endRowIndex": 1001}}]}', 400, 'Invalid dataFilter[0]: Range (Sheet1!1001) exceeds grid limits. Max rows: 1000, max columns: 26'),
    ('values', 'probe', b'{"dataFilters": [{"gridRange": {"sheetId": ID_SHEET1, "startColumnIndex": 26, "endColumnIndex": 27, "startRowIndex": 0}}]}', 400, 'Invalid dataFilter[0]: Range (Sheet1!AA1:AA) exceeds grid limits. Max rows: 1000, max columns: 26'),
    ('values', 'probe', b'{"dataFilters": [{"gridRange": {"sheetId": 5, "startRowIndex": -1}}]}', 400, 'Invalid dataFilter[0]: No grid with id: 5'),
    ('values', 'probe', b'{"dataFilters": [{"gridRange": {"sheetId": ID_SHEET1, "startRowIndex": 2147483647, "endRowIndex": 2147483647}}]}', 400, 'Invalid dataFilter[0]: Range ((empty) Sheet1!-2147483648:2147483647) exceeds grid limits. Max rows: 1000, max columns: 26'),
    ('sheet', 'probe', b'{"dataFilters": [{"gridRange": {"sheetId": 5, "startRowIndex": -1}}]}', 400, 'No sheet with id: 5'),
    ('values', 'probe', b'{"dataFilters": [{"gridRange": {"sheetId": ID_SHEET1, "startRowIndex": 1, "endRowIndex": 1}}, {"gridRange": {"sheetId": ID_SHEET1, "startRowIndex": -1}}]}', 400, 'Invalid dataFilter[1]: GridRange indexes must be >= 0'),
    ('values', 'probe', b'{"dataFilters": [{"a1Range": "Sheet1!A1"}], "bogus": null}', 400, 'Invalid JSON payload received. Unknown name "bogus": Cannot find field.'),
    ('values', 'probe', b'{"dataFilters": [{"developerMetadataLookup": {"metadataValue": 5}}]}', 400, "Invalid value at 'data_filters[0].developer_metadata_lookup.metadata_value.value' (TYPE_STRING), 5"),
    ('values', 'probe', b'{"valueRenderOption": 3}', 400, 'Invalid valueRenderOption: UNRECOGNIZED'),
    ('values', 'probe', b'{"majorDimension": 3}', 400, 'Must specify at least one dataFilter.'),
    ('values', 'probe', b'{"dataFilters": [{"a1Range": "Sheet1!A2000"}, {"gridRange": {"sheetId": ID_SHEET1, "startRowIndex": -1}}]}', 400, 'Invalid dataFilter[0]: Range (Sheet1!A2000) exceeds grid limits. Max rows: 1000, max columns: 26'),
    ('sheet', 'probe', b'{"dataFilters": [{"a1Range": "Sheet1!A2000"}]}', 400, 'Range (Sheet1!A2000) exceeds grid limits. Max rows: 1000, max columns: 26'),
    ('values', 'probe', b'{"dataFilters": [null, "abc"]}', 400, 'Invalid value at \'data_filters[0]\' (type.googleapis.com/google.apps.sheets.v4.DataFilter), "abc"'),
    ('values', 'probe', b'{"dataFilters": [{"a1Range": "Sheet1!A1"}, "abc"]}', 400, 'Invalid value at \'data_filters[1]\' (type.googleapis.com/google.apps.sheets.v4.DataFilter), "abc"'),
    ('values', 'probe', b'{"dataFilters": [{"developerMetadataLookup": {"locationMatchingStrategy": 7}}]}', 400, 'Invalid dataFilter[0]: A locationMatchingStrategy was specified, but no metadataLocation was specified: lookups must always specify a metadataLocation when specifying a locationMatchingStrategy.'),
    ('values', 'probe', b'{"dataFilters": [{"developerMetadataLookup": {"metadataLocation": {"dimensionRange": {"sheetId": ID_SHEET1, "dimension": 7, "startIndex": 0, "endIndex": 1}}}}]}', 500, 'Internal error encountered.'),
    ('values', 'probe', b'{"dataFilters": [{"gridRange": {"endColumnIndex": "x", "sheetId": "abc", "startRowIndex": 1.5}}]}', 400, 'Invalid value at \'data_filters[0].grid_range.end_column_index.value\' (TYPE_INT32), "x"\nInvalid value at \'data_filters[0].grid_range.sheet_id\' (TYPE_INT32), "abc"\nInvalid value at \'data_filters[0].grid_range.start_row_index.value\' (TYPE_INT32), 1.5'),
    ('values', 'probe', b'{"majorDimension": "NOPE", "dataFilters": [{"gridRange": {"startRowIndex": "a"}}, {"a1Range": 5}]}', 400, 'Invalid value at \'major_dimension\' (type.googleapis.com/google.apps.sheets.v4.Dimension), "NOPE"\nInvalid value at \'data_filters[0].grid_range.start_row_index.value\' (TYPE_INT32), "a"\nInvalid value at \'data_filters[1].a1_range\' (TYPE_STRING), 5'),
    ('values', 'probe', b'{"dataFilters": [{"developerMetadataLookup": {"locationType": 9}}]}', 500, 'Internal error encountered.'),
    ('values', 'probe', b'{"dataFilters": [{"developerMetadataLookup": {"visibility": 9}}]}', 500, 'Internal error encountered.'),
    ('values', 'probe', b'{"dataFilters": [{"developerMetadataLookup": {"metadataLocation": {"locationType": 9}}}]}', 200, None),
    ('values', 'probe', b'{"dataFilters": [{"developerMetadataLookup": {"metadataLocation": {"dimensionRange": {"sheetId": ID_SHEET1, "dimension": 7, "startIndex": 0, "endIndex": 3}}}}]}', 400, 'Invalid dataFilter[0]: DimensionRange must represent a single row or column.'),
    ('values', 'probe', b'{"dataFilters": [{"gridRange": {"sheetId": ID_DATA, "startRowIndex": "+1"}}]}', 200, [('Data!A2:Z1000', 'ROWS', {'gridRange': {'sheetId': 'ID_DATA', 'startRowIndex': 1}})]),
    ('values', 'probe', b'{"dataFilters": [{"gridRange": {"sheetId": ID_DATA, "startRowIndex": "01"}}]}', 200, [('Data!A2:Z1000', 'ROWS', {'gridRange': {'sheetId': 'ID_DATA', 'startRowIndex': 1}})]),
    ('values', 'probe', b'{"dataFilters": [{"gridRange": {"sheetId": ID_DATA, "startRowIndex": 1.0}}]}', 200, [('Data!A2:Z1000', 'ROWS', {'gridRange': {'sheetId': 'ID_DATA', 'startRowIndex': 1}})]),
    ('values', 'probe', b'{"dataFilters": [{"gridRange": {"sheetId": ID_DATA, "startRowIndex": {}}}]}', 200, [('Data!A1:Z1000', 'ROWS', {'gridRange': {'sheetId': 'ID_DATA', 'startRowIndex': 0}})]),
    ('values', 'probe', b'{"dataFilters": [{"gridRange": {"sheetId": ID_DATA, "startRowIndex": {"value": 1}}}]}', 200, [('Data!A2:Z1000', 'ROWS', {'gridRange': {'sheetId': 'ID_DATA', 'startRowIndex': 1}})]),
    ('values', 'probe', b'{"dataFilters": [{"gridRange": {"sheetId": ID_DATA, "startRowIndex": {"value": null}}}]}', 200, [('Data!A1:Z1000', 'ROWS', {'gridRange': {'sheetId': 'ID_DATA', 'startRowIndex': 0}})]),
    ('values', 'probe', b'{"dataFilters": [{"gridRange": {"sheetId": ID_DATA, "startRowIndex": "\\t1"}}]}', 200, [('Data!A2:Z1000', 'ROWS', {'gridRange': {'sheetId': 'ID_DATA', 'startRowIndex': 1}})]),
    ('values', 'probe', b'{"dataFilters": [{"gridRange": {"sheetId": ID_DATA, "endRowIndex": 2000}}]}', 200, [('Data!A1:Z1000', 'ROWS', {'gridRange': {'endRowIndex': 2000, 'sheetId': 'ID_DATA'}})]),
    ('values', 'probe', b'{"dataFilters": [{"gridRange": {"sheetId": ID_DATA, "startRowIndex": 999, "endRowIndex": 1001}}]}', 200, [('Data!A1000:Z1000', 'ROWS', {'gridRange': {'endRowIndex': 1001, 'sheetId': 'ID_DATA', 'startRowIndex': 999}})]),
    ('values', 'probe', b'{"dataFilters": [{"gridRange": {"sheetId": ID_DATA, "startRowIndex": 0, "endRowIndex": 0}}]}', 200, [('#REF!', 'ROWS', {'gridRange': {'endRowIndex': 0, 'sheetId': 'ID_DATA', 'startRowIndex': 0}})]),
    ('values', 'probe', b'{"dataFilters": [{"gridRange": {"sheetId": ID_DATA, "startColumnIndex": 3, "endColumnIndex": 3}}]}', 200, [('#REF!', 'ROWS', {'gridRange': {'endColumnIndex': 3, 'sheetId': 'ID_DATA', 'startColumnIndex': 3}})]),
    ('values', 'probe', b'{"dataFilters": [{"gridRange": {"sheetId": ID_DATA, "endRowIndex": 0}}]}', 200, [('#REF!', 'ROWS', {'gridRange': {'endRowIndex': 0, 'sheetId': 'ID_DATA'}})]),
    ('values', 'probe', b'{"dataFilters": [{"gridRange": {"sheetId": ID_DATA, "startRowIndex": 1, "endRowIndex": 1, "startColumnIndex": 1, "endColumnIndex": 1}}]}', 200, [('#REF!', 'ROWS', {'gridRange': {'endColumnIndex': 1, 'endRowIndex': 1, 'sheetId': 'ID_DATA', 'startColumnIndex': 1, 'startRowIndex': 1}})]),
    ('values', 'probe', b'{"dataFilters": [{"gridRange": {"sheetId": ID_DATA, "startRowIndex": null}}]}', 200, [('Data!A1:Z1000', 'ROWS', {'gridRange': {'sheetId': 'ID_DATA'}})]),
    ('values', 'probe', b'{"dataFilters": [{"gridRange": {"sheetId": "ID_DATA"}}]}', 200, [('Data!A1:Z1000', 'ROWS', {'gridRange': {'sheetId': 'ID_DATA'}})]),
    ('values', 'probe', b'{"data_filters": [{"grid_range": {"sheet_id": ID_DATA, "start_row_index": 1}}]}', 200, [('Data!A2:Z1000', 'ROWS', {'gridRange': {'sheetId': 'ID_DATA', 'startRowIndex': 1}})]),
    ('values', 'probe', b'{"dataFilters": [{"a1Range": null, "gridRange": {"sheetId": ID_DATA, "endRowIndex": 1, "endColumnIndex": 1}}]}', 200, [('Data!A1', 'ROWS', {'gridRange': {'endColumnIndex': 1, 'endRowIndex': 1, 'sheetId': 'ID_DATA'}})]),
    ('values', 'probe', b'{"dataFilters": [{"a1Range": "Data!A1"}, {"gridRange": {"sheetId": ID_DATA, "startRowIndex": 1, "endRowIndex": 1}}], "majorDimension": "COLUMNS"}', 200, [('Data!A1', 'COLUMNS', {'a1Range': 'Data!A1'}), ('#REF!', 'COLUMNS', {'gridRange': {'endRowIndex': 1, 'sheetId': 'ID_DATA', 'startRowIndex': 1}})]),
    ('values', 'probe', b'{"dataFilters": [{"developerMetadataLookup": {"metadataKey": "k"}}, {"gridRange": {"sheetId": ID_DATA, "endRowIndex": 1, "endColumnIndex": 1}}]}', 200, [('Data!A1', 'ROWS', {'gridRange': {'endColumnIndex': 1, 'endRowIndex': 1, 'sheetId': 'ID_DATA'}})]),
    ('values', 'probe', b'{"dataFilters": [{"a1Range": "Data!A1:B2"}], "majorDimension": 2}', 200, [('Data!A1:B2', 'COLUMNS', {'a1Range': 'Data!A1:B2'})]),
    ('values', 'probe', b'{"dataFilters": [{"a1Range": "Data!A1:B2"}], "majorDimension": "dimension-unspecified"}', 200, [('Data!A1:B2', 'ROWS', {'a1Range': 'Data!A1:B2'})]),
    ('values', 'probe', b'{"dataFilters":[{"a1Range":"Data!A1:B2"}],"majorDimension":"COLUMNS","majorDimension":"ROWS"}', 200, [('Data!A1:B2', 'ROWS', {'a1Range': 'Data!A1:B2'})]),
    ('sheet', 'probe', b'{"dataFilters": [{"gridRange": {"sheetId": ID_DATA, "startRowIndex": 1, "endRowIndex": 1, "startColumnIndex": 2, "endColumnIndex": 2}}], "includeGridData": true}', 200, [('Data', [{'startColumn': 2, 'startRow': 1}])]),
    ('sheet', 'probe', b'{"dataFilters": [{"gridRange": {"sheetId": ID_DATA, "startRowIndex": 1, "endRowIndex": 1}}], "includeGridData": true}', 200, [('Data', [{'columnMetadata': 26, 'startRow': 1}])]),
    ('sheet', 'probe', b'{"dataFilters": [{"gridRange": {"sheetId": ID_DATA, "startColumnIndex": 1, "endColumnIndex": 1}}], "includeGridData": true}', 200, [('Data', [{'rowMetadata': 1000, 'startColumn': 1}])]),
    ('sheet', 'probe', b'{"dataFilters": [{"developerMetadataLookup": {"metadataKey": "k"}}, {"a1Range": "Data!A1"}]}', 200, [('Data', None)]),
    ('sheet', 'probe', b'{"dataFilters": [{"developerMetadataLookup": {"metadataKey": "k"}}, {"developerMetadataLookup": {"metadataId": 3}}], "includeGridData": true}', 200, [('Sheet1', [{'columnMetadata': 26, 'rowMetadata': 1000}]), ('Data', [{'columnMetadata': 26, 'rowMetadata': 1000}]), ('R1C1', [{'columnMetadata': 26, 'rowMetadata': 1000}]), ('RC', [{'columnMetadata': 26, 'rowMetadata': 1000}]), ('A', [{'columnMetadata': 26, 'rowMetadata': 1000}])]),
    ('sheet', 'probe', b'{"dataFilters": [{"developerMetadataLookup": {"metadataKey": "k"}}], "includeGridData": true}', 200, [('Sheet1', [{'columnMetadata': 26, 'rowMetadata': 1000}]), ('Data', [{'columnMetadata': 26, 'rowMetadata': 1000}]), ('R1C1', [{'columnMetadata': 26, 'rowMetadata': 1000}]), ('RC', [{'columnMetadata': 26, 'rowMetadata': 1000}]), ('A', [{'columnMetadata': 26, 'rowMetadata': 1000}])]),
    ('values', 'probe', b'{"dataFilters": [{"gridRange": {"sheetId": ID_SHEET1, "startRowIndex": 1000, "endRowIndex": 1005}}]}', 400, 'Invalid dataFilter[0]: Range (Sheet1!1001:1005) exceeds grid limits. Max rows: 1000, max columns: 26'),
    ('values', 'probe', b'{"dataFilters": [{"gridRange": {"sheetId": ID_SHEET1, "startRowIndex": 1000, "startColumnIndex": 1}}]}', 400, 'Invalid dataFilter[0]: Range (Sheet1!B1001:) exceeds grid limits. Max rows: 1000, max columns: 26'),
    ('values', 'probe', b'{"dataFilters": [{"gridRange": {"sheetId": ID_SHEET1, "startRowIndex": 1000, "endColumnIndex": 3}}]}', 400, 'Invalid dataFilter[0]: Range (Sheet1!1001:C) exceeds grid limits. Max rows: 1000, max columns: 26'),
    ('values', 'probe', b'{"dataFilters": [{"gridRange": {"sheetId": ID_SHEET1, "startRowIndex": 1000, "endRowIndex": 1005, "startColumnIndex": 1}}]}', 400, 'Invalid dataFilter[0]: Range (Sheet1!B1001:1005) exceeds grid limits. Max rows: 1000, max columns: 26'),
    ('values', 'probe', b'{"dataFilters": [{"gridRange": {"sheetId": ID_SHEET1, "startRowIndex": 1000, "endRowIndex": 1005, "startColumnIndex": 1, "endColumnIndex": 3}}]}', 400, 'Invalid dataFilter[0]: Range (Sheet1!B1001:C1005) exceeds grid limits. Max rows: 1000, max columns: 26'),
    ('values', 'probe', b'{"dataFilters": [{"gridRange": {"sheetId": ID_SHEET1, "startColumnIndex": 26, "endColumnIndex": 28}}]}', 400, 'Invalid dataFilter[0]: Range (Sheet1!AA:AB) exceeds grid limits. Max rows: 1000, max columns: 26'),
    ('values', 'probe', b'{"dataFilters": [{"gridRange": {"sheetId": ID_SHEET1, "startColumnIndex": 26, "startRowIndex": 1}}]}', 400, 'Invalid dataFilter[0]: Range (Sheet1!AA2:) exceeds grid limits. Max rows: 1000, max columns: 26'),
    ('values', 'probe', b'{"dataFilters": [{"gridRange": {"sheetId": ID_SHEET1, "startColumnIndex": 26, "endColumnIndex": 28, "startRowIndex": 1}}]}', 400, 'Invalid dataFilter[0]: Range (Sheet1!AA2:AB) exceeds grid limits. Max rows: 1000, max columns: 26'),
    ('values', 'probe', b'{"dataFilters": [{"gridRange": {"sheetId": ID_SHEET1, "startColumnIndex": 26, "endColumnIndex": 28, "endRowIndex": 5}}]}', 400, 'Invalid dataFilter[0]: Range (Sheet1!AA:AB5) exceeds grid limits. Max rows: 1000, max columns: 26'),
    ('values', 'probe', b'{"dataFilters": [{"gridRange": {"sheetId": ID_SHEET1, "startColumnIndex": 26, "startRowIndex": 1, "endRowIndex": 5}}]}', 400, 'Invalid dataFilter[0]: Range (Sheet1!AA2:5) exceeds grid limits. Max rows: 1000, max columns: 26'),
    ('values', 'probe', b'{"dataFilters": [{"gridRange": {"sheetId": ID_SHEET1, "startColumnIndex": 26, "endColumnIndex": 28, "startRowIndex": 1, "endRowIndex": 5}}]}', 400, 'Invalid dataFilter[0]: Range (Sheet1!AA2:AB5) exceeds grid limits. Max rows: 1000, max columns: 26'),
    ('values', 'probe', b'{"dataFilters": [{"a1Range": "Sheet1!A1"}], "bogus": {"majorDimension": "NOPE", "x": [1, {"y": 2}]}, "valueRenderOption": "NOPE"}', 400, 'Invalid JSON payload received. Unknown name "bogus": Cannot find field.\nInvalid value at \'value_render_option\' (type.googleapis.com/google.apps.sheets.v4.ValueRenderOption), "NOPE"'),
    ('values', 'probe', b'{"bogus": [{"majorDimension": "NOPE"}], "dataFilters": [{"a1Range": "Sheet1!A1"}]}', 400, 'Invalid JSON payload received. Unknown name "bogus": Cannot find field.'),
    ('values', 'probe', b'{"dataFilters": [{"a1Range": "Sheet1!A1", "bogus": {"gridRange": "abc"}}]}', 400, 'Invalid JSON payload received. Unknown name "bogus" at \'data_filters[0]\': Cannot find field.'),
    ('values', 'probe', b'{"dataFilters": [{"developerMetadataLookup": {"locationMatchingStrategy": "EXACT_LOCATION"}}]}', 400, 'Invalid dataFilter[0]: A locationMatchingStrategy was specified, but no metadataLocation was specified: lookups must always specify a metadataLocation when specifying a locationMatchingStrategy.'),
    ('values', 'probe', b'{"dataFilters": [{"developerMetadataLookup": {"locationMatchingStrategy": 9}}]}', 400, 'Invalid dataFilter[0]: A locationMatchingStrategy was specified, but no metadataLocation was specified: lookups must always specify a metadataLocation when specifying a locationMatchingStrategy.'),
    ('values', 'probe', b'{"dataFilters": [{"developerMetadataLookup": {"metadataLocation": {}}}]}', 200, None),
    ('values', 'probe', b'{"dataFilters": [{"developerMetadataLookup": {"metadataLocation": {}, "locationMatchingStrategy": "EXACT_LOCATION"}}]}', 400, 'Invalid dataFilter[0]: A locationMatchingStrategy was specified, but no metadataLocation was specified: lookups must always specify a metadataLocation when specifying a locationMatchingStrategy.'),
    ('values', 'probe', b'{"dataFilters": [{"developerMetadataLookup": {"metadataLocation": {}, "locationMatchingStrategy": "INTERSECTING_LOCATION"}}]}', 400, 'Invalid dataFilter[0]: A locationMatchingStrategy was specified, but no metadataLocation was specified: lookups must always specify a metadataLocation when specifying a locationMatchingStrategy.'),
    ('values', 'probe', b'{"dataFilters": [{"developerMetadataLookup": {"metadataLocation": {}, "locationMatchingStrategy": 9}}]}', 400, 'Invalid dataFilter[0]: A locationMatchingStrategy was specified, but no metadataLocation was specified: lookups must always specify a metadataLocation when specifying a locationMatchingStrategy.'),
    ('values', 'probe', b'{"dataFilters": [{"developerMetadataLookup": {"metadataLocation": {"spreadsheet": true}, "locationMatchingStrategy": "EXACT_LOCATION"}}]}', 200, None),
    ('values', 'probe', b'{"dataFilters": [{"developerMetadataLookup": {"metadataLocation": {"spreadsheet": true}, "locationMatchingStrategy": "INTERSECTING_LOCATION"}}]}', 400, 'Invalid dataFilter[0]: DeveloperMetadataLookup.spreadsheet is true, but locationMatchingStrategy was specified as INTERSECTING.'),
    ('values', 'probe', b'{"dataFilters": [{"developerMetadataLookup": {"metadataLocation": {"spreadsheet": true}, "locationMatchingStrategy": 9}}]}', 500, 'Internal error encountered.'),
    ('values', 'probe', b'{"dataFilters": [{"developerMetadataLookup": {"metadataLocation": {"spreadsheet": false}}}]}', 200, None),
    ('values', 'probe', b'{"dataFilters": [{"developerMetadataLookup": {"metadataLocation": {"spreadsheet": false}, "locationMatchingStrategy": "EXACT_LOCATION"}}]}', 200, None),
    ('values', 'probe', b'{"dataFilters": [{"developerMetadataLookup": {"metadataLocation": {"spreadsheet": false}, "locationMatchingStrategy": "INTERSECTING_LOCATION"}}]}', 200, None),
    ('values', 'probe', b'{"dataFilters": [{"developerMetadataLookup": {"metadataLocation": {"spreadsheet": false}, "locationMatchingStrategy": 9}}]}', 500, 'Internal error encountered.'),
    ('values', 'probe', b'{"dataFilters": [{"developerMetadataLookup": {"metadataLocation": {"sheetId": ID_DATA}}}]}', 200, None),
    ('values', 'probe', b'{"dataFilters": [{"developerMetadataLookup": {"metadataLocation": {"sheetId": ID_DATA}, "locationMatchingStrategy": "EXACT_LOCATION"}}]}', 200, None),
    ('values', 'probe', b'{"dataFilters": [{"developerMetadataLookup": {"metadataLocation": {"sheetId": ID_DATA}, "locationMatchingStrategy": "INTERSECTING_LOCATION"}}]}', 200, None),
    ('values', 'probe', b'{"dataFilters": [{"developerMetadataLookup": {"metadataLocation": {"sheetId": ID_DATA}, "locationMatchingStrategy": 9}}]}', 500, 'Internal error encountered.'),
    ('values', 'probe', b'{"dataFilters": [{"developerMetadataLookup": {"metadataLocation": {"sheetId": 5}, "locationMatchingStrategy": "EXACT_LOCATION"}}]}', 400, 'Invalid dataFilter[0]: No grid with id: 5'),
    ('values', 'probe', b'{"dataFilters": [{"developerMetadataLookup": {"metadataLocation": {"sheetId": 5}, "locationMatchingStrategy": "INTERSECTING_LOCATION"}}]}', 400, 'Invalid dataFilter[0]: No grid with id: 5'),
    ('values', 'probe', b'{"dataFilters": [{"developerMetadataLookup": {"metadataLocation": {"sheetId": 5}, "locationMatchingStrategy": 9}}]}', 400, 'Invalid dataFilter[0]: No grid with id: 5'),
    ('values', 'probe', b'{"dataFilters": [{"developerMetadataLookup": {"metadataLocation": {"dimensionRange": {"sheetId": ID_DATA, "dimension": "ROWS", "startIndex": 0, "endIndex": 1}}}}]}', 200, None),
    ('values', 'probe', b'{"dataFilters": [{"developerMetadataLookup": {"metadataLocation": {"dimensionRange": {"sheetId": ID_DATA, "dimension": "ROWS", "startIndex": 0, "endIndex": 1}}, "locationMatchingStrategy": "EXACT_LOCATION"}}]}', 200, None),
    ('values', 'probe', b'{"dataFilters": [{"developerMetadataLookup": {"metadataLocation": {"dimensionRange": {"sheetId": ID_DATA, "dimension": "ROWS", "startIndex": 0, "endIndex": 1}}, "locationMatchingStrategy": "INTERSECTING_LOCATION"}}]}', 200, None),
    ('values', 'probe', b'{"dataFilters": [{"developerMetadataLookup": {"metadataLocation": {"dimensionRange": {"sheetId": ID_DATA, "dimension": "ROWS", "startIndex": 0, "endIndex": 1}}, "locationMatchingStrategy": 9}}]}', 500, 'Internal error encountered.'),
    ('values', 'probe', b'{"dataFilters": [{"developerMetadataLookup": {"metadataLocation": {"dimensionRange": {"sheetId": ID_DATA, "dimension": "COLUMNS", "startIndex": 0, "endIndex": 1}}}}]}', 200, None),
    ('values', 'probe', b'{"dataFilters": [{"developerMetadataLookup": {"metadataLocation": {"dimensionRange": {"sheetId": ID_DATA, "dimension": "COLUMNS", "startIndex": 0, "endIndex": 1}}, "locationMatchingStrategy": "EXACT_LOCATION"}}]}', 200, None),
    ('values', 'probe', b'{"dataFilters": [{"developerMetadataLookup": {"metadataLocation": {"dimensionRange": {"sheetId": ID_DATA, "dimension": "COLUMNS", "startIndex": 0, "endIndex": 1}}, "locationMatchingStrategy": "INTERSECTING_LOCATION"}}]}', 200, None),
    ('values', 'probe', b'{"dataFilters": [{"developerMetadataLookup": {"metadataLocation": {"dimensionRange": {"sheetId": ID_DATA, "dimension": "COLUMNS", "startIndex": 0, "endIndex": 1}}, "locationMatchingStrategy": 9}}]}', 500, 'Internal error encountered.'),
    ('values', 'probe', b'{"dataFilters": [{"developerMetadataLookup": {"locationType": "ROW"}}]}', 200, None),
    ('values', 'probe', b'{"dataFilters": [{"developerMetadataLookup": {"locationType": "ROW", "locationMatchingStrategy": "EXACT_LOCATION"}}]}', 400, 'Invalid dataFilter[0]: The locationMatchingStrategy was specified as EXACT_LOCATION, but a locationType was also specified: lookups cannot limit by a location type when matching an exact location.'),
    ('values', 'probe', b'{"dataFilters": [{"developerMetadataLookup": {"locationType": "ROW", "locationMatchingStrategy": "INTERSECTING_LOCATION"}}]}', 400, 'Invalid dataFilter[0]: A locationMatchingStrategy was specified, but no metadataLocation was specified: lookups must always specify a metadataLocation when specifying a locationMatchingStrategy.'),
    ('values', 'probe', b'{"dataFilters": [{"developerMetadataLookup": {"locationType": "ROW", "locationMatchingStrategy": 9}}]}', 400, 'Invalid dataFilter[0]: A locationMatchingStrategy was specified, but no metadataLocation was specified: lookups must always specify a metadataLocation when specifying a locationMatchingStrategy.'),
    ('values', 'probe', b'{"dataFilters": [{"developerMetadataLookup": {"locationType": "ROW", "metadataLocation": {}}}]}', 200, None),
    ('values', 'probe', b'{"dataFilters": [{"developerMetadataLookup": {"locationType": "ROW", "metadataLocation": {}, "locationMatchingStrategy": "EXACT_LOCATION"}}]}', 400, 'Invalid dataFilter[0]: The locationMatchingStrategy was specified as EXACT_LOCATION, but a locationType was also specified: lookups cannot limit by a location type when matching an exact location.'),
    ('values', 'probe', b'{"dataFilters": [{"developerMetadataLookup": {"locationType": "ROW", "metadataLocation": {}, "locationMatchingStrategy": "INTERSECTING_LOCATION"}}]}', 400, 'Invalid dataFilter[0]: A locationMatchingStrategy was specified, but no metadataLocation was specified: lookups must always specify a metadataLocation when specifying a locationMatchingStrategy.'),
    ('values', 'probe', b'{"dataFilters": [{"developerMetadataLookup": {"locationType": "ROW", "metadataLocation": {}, "locationMatchingStrategy": 9}}]}', 400, 'Invalid dataFilter[0]: A locationMatchingStrategy was specified, but no metadataLocation was specified: lookups must always specify a metadataLocation when specifying a locationMatchingStrategy.'),
    ('values', 'probe', b'{"dataFilters": [{"developerMetadataLookup": {"locationType": "ROW", "metadataLocation": {"spreadsheet": true}}}]}', 500, 'Internal error encountered.'),
    ('values', 'probe', b'{"dataFilters": [{"developerMetadataLookup": {"locationType": "ROW", "metadataLocation": {"spreadsheet": true}, "locationMatchingStrategy": "EXACT_LOCATION"}}]}', 400, 'Invalid dataFilter[0]: The locationMatchingStrategy was specified as EXACT_LOCATION, but a locationType was also specified: lookups cannot limit by a location type when matching an exact location.'),
    ('values', 'probe', b'{"dataFilters": [{"developerMetadataLookup": {"locationType": "ROW", "metadataLocation": {"spreadsheet": true}, "locationMatchingStrategy": "INTERSECTING_LOCATION"}}]}', 400, 'Invalid dataFilter[0]: DeveloperMetadataLookup.spreadsheet is true, but locationMatchingStrategy was specified as INTERSECTING.'),
    ('values', 'probe', b'{"dataFilters": [{"developerMetadataLookup": {"locationType": "ROW", "metadataLocation": {"spreadsheet": true}, "locationMatchingStrategy": 9}}]}', 500, 'Internal error encountered.'),
    ('values', 'probe', b'{"dataFilters": [{"developerMetadataLookup": {"locationType": "ROW", "metadataLocation": {"spreadsheet": false}}}]}', 500, 'Internal error encountered.'),
    ('values', 'probe', b'{"dataFilters": [{"developerMetadataLookup": {"locationType": "ROW", "metadataLocation": {"spreadsheet": false}, "locationMatchingStrategy": "EXACT_LOCATION"}}]}', 400, 'Invalid dataFilter[0]: The locationMatchingStrategy was specified as EXACT_LOCATION, but a locationType was also specified: lookups cannot limit by a location type when matching an exact location.'),
    ('values', 'probe', b'{"dataFilters": [{"developerMetadataLookup": {"locationType": "ROW", "metadataLocation": {"spreadsheet": false}, "locationMatchingStrategy": "INTERSECTING_LOCATION"}}]}', 500, 'Internal error encountered.'),
    ('values', 'probe', b'{"dataFilters": [{"developerMetadataLookup": {"locationType": "ROW", "metadataLocation": {"spreadsheet": false}, "locationMatchingStrategy": 9}}]}', 500, 'Internal error encountered.'),
    ('values', 'probe', b'{"dataFilters": [{"developerMetadataLookup": {"locationType": "ROW", "metadataLocation": {"sheetId": ID_DATA}}}]}', 200, None),
    ('values', 'probe', b'{"dataFilters": [{"developerMetadataLookup": {"locationType": "ROW", "metadataLocation": {"sheetId": ID_DATA}, "locationMatchingStrategy": "EXACT_LOCATION"}}]}', 400, 'Invalid dataFilter[0]: The locationMatchingStrategy was specified as EXACT_LOCATION, but a locationType was also specified: lookups cannot limit by a location type when matching an exact location.'),
    ('values', 'probe', b'{"dataFilters": [{"developerMetadataLookup": {"locationType": "ROW", "metadataLocation": {"sheetId": ID_DATA}, "locationMatchingStrategy": "INTERSECTING_LOCATION"}}]}', 200, None),
    ('values', 'probe', b'{"dataFilters": [{"developerMetadataLookup": {"locationType": "ROW", "metadataLocation": {"sheetId": ID_DATA}, "locationMatchingStrategy": 9}}]}', 500, 'Internal error encountered.'),
    ('values', 'probe', b'{"dataFilters": [{"developerMetadataLookup": {"locationType": "ROW", "metadataLocation": {"sheetId": 5}}}]}', 400, 'Invalid dataFilter[0]: No grid with id: 5'),
    ('values', 'probe', b'{"dataFilters": [{"developerMetadataLookup": {"locationType": "ROW", "metadataLocation": {"sheetId": 5}, "locationMatchingStrategy": "EXACT_LOCATION"}}]}', 400, 'Invalid dataFilter[0]: The locationMatchingStrategy was specified as EXACT_LOCATION, but a locationType was also specified: lookups cannot limit by a location type when matching an exact location.'),
    ('values', 'probe', b'{"dataFilters": [{"developerMetadataLookup": {"locationType": "ROW", "metadataLocation": {"sheetId": 5}, "locationMatchingStrategy": "INTERSECTING_LOCATION"}}]}', 400, 'Invalid dataFilter[0]: No grid with id: 5'),
    ('values', 'probe', b'{"dataFilters": [{"developerMetadataLookup": {"locationType": "ROW", "metadataLocation": {"sheetId": 5}, "locationMatchingStrategy": 9}}]}', 400, 'Invalid dataFilter[0]: No grid with id: 5'),
    ('values', 'probe', b'{"dataFilters": [{"developerMetadataLookup": {"locationType": "ROW", "metadataLocation": {"dimensionRange": {"sheetId": ID_DATA, "dimension": "ROWS", "startIndex": 0, "endIndex": 1}}}}]}', 200, None),
    ('values', 'probe', b'{"dataFilters": [{"developerMetadataLookup": {"locationType": "ROW", "metadataLocation": {"dimensionRange": {"sheetId": ID_DATA, "dimension": "ROWS", "startIndex": 0, "endIndex": 1}}, "locationMatchingStrategy": "EXACT_LOCATION"}}]}', 400, 'Invalid dataFilter[0]: The locationMatchingStrategy was specified as EXACT_LOCATION, but a locationType was also specified: lookups cannot limit by a location type when matching an exact location.'),
    ('values', 'probe', b'{"dataFilters": [{"developerMetadataLookup": {"locationType": "ROW", "metadataLocation": {"dimensionRange": {"sheetId": ID_DATA, "dimension": "ROWS", "startIndex": 0, "endIndex": 1}}, "locationMatchingStrategy": "INTERSECTING_LOCATION"}}]}', 200, None),
    ('values', 'probe', b'{"dataFilters": [{"developerMetadataLookup": {"locationType": "ROW", "metadataLocation": {"dimensionRange": {"sheetId": ID_DATA, "dimension": "ROWS", "startIndex": 0, "endIndex": 1}}, "locationMatchingStrategy": 9}}]}', 500, 'Internal error encountered.'),
    ('values', 'probe', b'{"dataFilters": [{"developerMetadataLookup": {"locationType": "ROW", "metadataLocation": {"dimensionRange": {"sheetId": ID_DATA, "dimension": "COLUMNS", "startIndex": 0, "endIndex": 1}}}}]}', 200, None),
    ('values', 'probe', b'{"dataFilters": [{"developerMetadataLookup": {"locationType": "ROW", "metadataLocation": {"dimensionRange": {"sheetId": ID_DATA, "dimension": "COLUMNS", "startIndex": 0, "endIndex": 1}}, "locationMatchingStrategy": "EXACT_LOCATION"}}]}', 400, 'Invalid dataFilter[0]: The locationMatchingStrategy was specified as EXACT_LOCATION, but a locationType was also specified: lookups cannot limit by a location type when matching an exact location.'),
    ('values', 'probe', b'{"dataFilters": [{"developerMetadataLookup": {"locationType": "ROW", "metadataLocation": {"dimensionRange": {"sheetId": ID_DATA, "dimension": "COLUMNS", "startIndex": 0, "endIndex": 1}}, "locationMatchingStrategy": "INTERSECTING_LOCATION"}}]}', 200, None),
    ('values', 'probe', b'{"dataFilters": [{"developerMetadataLookup": {"locationType": "ROW", "metadataLocation": {"dimensionRange": {"sheetId": ID_DATA, "dimension": "COLUMNS", "startIndex": 0, "endIndex": 1}}, "locationMatchingStrategy": 9}}]}', 500, 'Internal error encountered.'),
    ('values', 'probe', b'{"dataFilters": [{"developerMetadataLookup": {"locationType": "COLUMN"}}]}', 200, None),
    ('values', 'probe', b'{"dataFilters": [{"developerMetadataLookup": {"locationType": "COLUMN", "locationMatchingStrategy": "EXACT_LOCATION"}}]}', 400, 'Invalid dataFilter[0]: The locationMatchingStrategy was specified as EXACT_LOCATION, but a locationType was also specified: lookups cannot limit by a location type when matching an exact location.'),
    ('values', 'probe', b'{"dataFilters": [{"developerMetadataLookup": {"locationType": "COLUMN", "locationMatchingStrategy": "INTERSECTING_LOCATION"}}]}', 400, 'Invalid dataFilter[0]: A locationMatchingStrategy was specified, but no metadataLocation was specified: lookups must always specify a metadataLocation when specifying a locationMatchingStrategy.'),
    ('values', 'probe', b'{"dataFilters": [{"developerMetadataLookup": {"locationType": "COLUMN", "locationMatchingStrategy": 9}}]}', 400, 'Invalid dataFilter[0]: A locationMatchingStrategy was specified, but no metadataLocation was specified: lookups must always specify a metadataLocation when specifying a locationMatchingStrategy.'),
    ('values', 'probe', b'{"dataFilters": [{"developerMetadataLookup": {"locationType": "COLUMN", "metadataLocation": {}}}]}', 200, None),
    ('values', 'probe', b'{"dataFilters": [{"developerMetadataLookup": {"locationType": "COLUMN", "metadataLocation": {}, "locationMatchingStrategy": "EXACT_LOCATION"}}]}', 400, 'Invalid dataFilter[0]: The locationMatchingStrategy was specified as EXACT_LOCATION, but a locationType was also specified: lookups cannot limit by a location type when matching an exact location.'),
    ('values', 'probe', b'{"dataFilters": [{"developerMetadataLookup": {"locationType": "COLUMN", "metadataLocation": {}, "locationMatchingStrategy": "INTERSECTING_LOCATION"}}]}', 400, 'Invalid dataFilter[0]: A locationMatchingStrategy was specified, but no metadataLocation was specified: lookups must always specify a metadataLocation when specifying a locationMatchingStrategy.'),
    ('values', 'probe', b'{"dataFilters": [{"developerMetadataLookup": {"locationType": "COLUMN", "metadataLocation": {}, "locationMatchingStrategy": 9}}]}', 400, 'Invalid dataFilter[0]: A locationMatchingStrategy was specified, but no metadataLocation was specified: lookups must always specify a metadataLocation when specifying a locationMatchingStrategy.'),
    ('values', 'probe', b'{"dataFilters": [{"developerMetadataLookup": {"locationType": "COLUMN", "metadataLocation": {"spreadsheet": true}}}]}', 500, 'Internal error encountered.'),
    ('values', 'probe', b'{"dataFilters": [{"developerMetadataLookup": {"locationType": "COLUMN", "metadataLocation": {"spreadsheet": true}, "locationMatchingStrategy": "EXACT_LOCATION"}}]}', 400, 'Invalid dataFilter[0]: The locationMatchingStrategy was specified as EXACT_LOCATION, but a locationType was also specified: lookups cannot limit by a location type when matching an exact location.'),
    ('values', 'probe', b'{"dataFilters": [{"developerMetadataLookup": {"locationType": "COLUMN", "metadataLocation": {"spreadsheet": true}, "locationMatchingStrategy": "INTERSECTING_LOCATION"}}]}', 400, 'Invalid dataFilter[0]: DeveloperMetadataLookup.spreadsheet is true, but locationMatchingStrategy was specified as INTERSECTING.'),
    ('values', 'probe', b'{"dataFilters": [{"developerMetadataLookup": {"locationType": "COLUMN", "metadataLocation": {"spreadsheet": true}, "locationMatchingStrategy": 9}}]}', 500, 'Internal error encountered.'),
    ('values', 'probe', b'{"dataFilters": [{"developerMetadataLookup": {"locationType": "COLUMN", "metadataLocation": {"spreadsheet": false}}}]}', 500, 'Internal error encountered.'),
    ('values', 'probe', b'{"dataFilters": [{"developerMetadataLookup": {"locationType": "COLUMN", "metadataLocation": {"spreadsheet": false}, "locationMatchingStrategy": "EXACT_LOCATION"}}]}', 400, 'Invalid dataFilter[0]: The locationMatchingStrategy was specified as EXACT_LOCATION, but a locationType was also specified: lookups cannot limit by a location type when matching an exact location.'),
    ('values', 'probe', b'{"dataFilters": [{"developerMetadataLookup": {"locationType": "COLUMN", "metadataLocation": {"spreadsheet": false}, "locationMatchingStrategy": "INTERSECTING_LOCATION"}}]}', 500, 'Internal error encountered.'),
    ('values', 'probe', b'{"dataFilters": [{"developerMetadataLookup": {"locationType": "COLUMN", "metadataLocation": {"spreadsheet": false}, "locationMatchingStrategy": 9}}]}', 500, 'Internal error encountered.'),
    ('values', 'probe', b'{"dataFilters": [{"developerMetadataLookup": {"locationType": "COLUMN", "metadataLocation": {"sheetId": ID_DATA}}}]}', 200, None),
    ('values', 'probe', b'{"dataFilters": [{"developerMetadataLookup": {"locationType": "COLUMN", "metadataLocation": {"sheetId": ID_DATA}, "locationMatchingStrategy": "EXACT_LOCATION"}}]}', 400, 'Invalid dataFilter[0]: The locationMatchingStrategy was specified as EXACT_LOCATION, but a locationType was also specified: lookups cannot limit by a location type when matching an exact location.'),
    ('values', 'probe', b'{"dataFilters": [{"developerMetadataLookup": {"locationType": "COLUMN", "metadataLocation": {"sheetId": ID_DATA}, "locationMatchingStrategy": "INTERSECTING_LOCATION"}}]}', 200, None),
    ('values', 'probe', b'{"dataFilters": [{"developerMetadataLookup": {"locationType": "COLUMN", "metadataLocation": {"sheetId": ID_DATA}, "locationMatchingStrategy": 9}}]}', 500, 'Internal error encountered.'),
    ('values', 'probe', b'{"dataFilters": [{"developerMetadataLookup": {"locationType": "COLUMN", "metadataLocation": {"sheetId": 5}}}]}', 400, 'Invalid dataFilter[0]: No grid with id: 5'),
    ('values', 'probe', b'{"dataFilters": [{"developerMetadataLookup": {"locationType": "COLUMN", "metadataLocation": {"sheetId": 5}, "locationMatchingStrategy": "EXACT_LOCATION"}}]}', 400, 'Invalid dataFilter[0]: The locationMatchingStrategy was specified as EXACT_LOCATION, but a locationType was also specified: lookups cannot limit by a location type when matching an exact location.'),
    ('values', 'probe', b'{"dataFilters": [{"developerMetadataLookup": {"locationType": "COLUMN", "metadataLocation": {"sheetId": 5}, "locationMatchingStrategy": "INTERSECTING_LOCATION"}}]}', 400, 'Invalid dataFilter[0]: No grid with id: 5'),
    ('values', 'probe', b'{"dataFilters": [{"developerMetadataLookup": {"locationType": "COLUMN", "metadataLocation": {"sheetId": 5}, "locationMatchingStrategy": 9}}]}', 400, 'Invalid dataFilter[0]: No grid with id: 5'),
    ('values', 'probe', b'{"dataFilters": [{"developerMetadataLookup": {"locationType": "COLUMN", "metadataLocation": {"dimensionRange": {"sheetId": ID_DATA, "dimension": "ROWS", "startIndex": 0, "endIndex": 1}}}}]}', 200, None),
    ('values', 'probe', b'{"dataFilters": [{"developerMetadataLookup": {"locationType": "COLUMN", "metadataLocation": {"dimensionRange": {"sheetId": ID_DATA, "dimension": "ROWS", "startIndex": 0, "endIndex": 1}}, "locationMatchingStrategy": "EXACT_LOCATION"}}]}', 400, 'Invalid dataFilter[0]: The locationMatchingStrategy was specified as EXACT_LOCATION, but a locationType was also specified: lookups cannot limit by a location type when matching an exact location.'),
    ('values', 'probe', b'{"dataFilters": [{"developerMetadataLookup": {"locationType": "COLUMN", "metadataLocation": {"dimensionRange": {"sheetId": ID_DATA, "dimension": "ROWS", "startIndex": 0, "endIndex": 1}}, "locationMatchingStrategy": "INTERSECTING_LOCATION"}}]}', 200, None),
    ('values', 'probe', b'{"dataFilters": [{"developerMetadataLookup": {"locationType": "COLUMN", "metadataLocation": {"dimensionRange": {"sheetId": ID_DATA, "dimension": "ROWS", "startIndex": 0, "endIndex": 1}}, "locationMatchingStrategy": 9}}]}', 500, 'Internal error encountered.'),
    ('values', 'probe', b'{"dataFilters": [{"developerMetadataLookup": {"locationType": "COLUMN", "metadataLocation": {"dimensionRange": {"sheetId": ID_DATA, "dimension": "COLUMNS", "startIndex": 0, "endIndex": 1}}}}]}', 200, None),
    ('values', 'probe', b'{"dataFilters": [{"developerMetadataLookup": {"locationType": "COLUMN", "metadataLocation": {"dimensionRange": {"sheetId": ID_DATA, "dimension": "COLUMNS", "startIndex": 0, "endIndex": 1}}, "locationMatchingStrategy": "EXACT_LOCATION"}}]}', 400, 'Invalid dataFilter[0]: The locationMatchingStrategy was specified as EXACT_LOCATION, but a locationType was also specified: lookups cannot limit by a location type when matching an exact location.'),
    ('values', 'probe', b'{"dataFilters": [{"developerMetadataLookup": {"locationType": "COLUMN", "metadataLocation": {"dimensionRange": {"sheetId": ID_DATA, "dimension": "COLUMNS", "startIndex": 0, "endIndex": 1}}, "locationMatchingStrategy": "INTERSECTING_LOCATION"}}]}', 200, None),
    ('values', 'probe', b'{"dataFilters": [{"developerMetadataLookup": {"locationType": "COLUMN", "metadataLocation": {"dimensionRange": {"sheetId": ID_DATA, "dimension": "COLUMNS", "startIndex": 0, "endIndex": 1}}, "locationMatchingStrategy": 9}}]}', 500, 'Internal error encountered.'),
    ('values', 'probe', b'{"dataFilters": [{"developerMetadataLookup": {"locationType": "SHEET"}}]}', 200, None),
    ('values', 'probe', b'{"dataFilters": [{"developerMetadataLookup": {"locationType": "SHEET", "locationMatchingStrategy": "EXACT_LOCATION"}}]}', 400, 'Invalid dataFilter[0]: The locationMatchingStrategy was specified as EXACT_LOCATION, but a locationType was also specified: lookups cannot limit by a location type when matching an exact location.'),
    ('values', 'probe', b'{"dataFilters": [{"developerMetadataLookup": {"locationType": "SHEET", "locationMatchingStrategy": "INTERSECTING_LOCATION"}}]}', 400, 'Invalid dataFilter[0]: A locationMatchingStrategy was specified, but no metadataLocation was specified: lookups must always specify a metadataLocation when specifying a locationMatchingStrategy.'),
    ('values', 'probe', b'{"dataFilters": [{"developerMetadataLookup": {"locationType": "SHEET", "locationMatchingStrategy": 9}}]}', 400, 'Invalid dataFilter[0]: A locationMatchingStrategy was specified, but no metadataLocation was specified: lookups must always specify a metadataLocation when specifying a locationMatchingStrategy.'),
    ('values', 'probe', b'{"dataFilters": [{"developerMetadataLookup": {"locationType": "SHEET", "metadataLocation": {}}}]}', 200, None),
    ('values', 'probe', b'{"dataFilters": [{"developerMetadataLookup": {"locationType": "SHEET", "metadataLocation": {}, "locationMatchingStrategy": "EXACT_LOCATION"}}]}', 400, 'Invalid dataFilter[0]: The locationMatchingStrategy was specified as EXACT_LOCATION, but a locationType was also specified: lookups cannot limit by a location type when matching an exact location.'),
    ('values', 'probe', b'{"dataFilters": [{"developerMetadataLookup": {"locationType": "SHEET", "metadataLocation": {}, "locationMatchingStrategy": "INTERSECTING_LOCATION"}}]}', 400, 'Invalid dataFilter[0]: A locationMatchingStrategy was specified, but no metadataLocation was specified: lookups must always specify a metadataLocation when specifying a locationMatchingStrategy.'),
    ('values', 'probe', b'{"dataFilters": [{"developerMetadataLookup": {"locationType": "SHEET", "metadataLocation": {}, "locationMatchingStrategy": 9}}]}', 400, 'Invalid dataFilter[0]: A locationMatchingStrategy was specified, but no metadataLocation was specified: lookups must always specify a metadataLocation when specifying a locationMatchingStrategy.'),
    ('values', 'probe', b'{"dataFilters": [{"developerMetadataLookup": {"locationType": "SHEET", "metadataLocation": {"spreadsheet": true}}}]}', 500, 'Internal error encountered.'),
    ('values', 'probe', b'{"dataFilters": [{"developerMetadataLookup": {"locationType": "SHEET", "metadataLocation": {"spreadsheet": true}, "locationMatchingStrategy": "EXACT_LOCATION"}}]}', 400, 'Invalid dataFilter[0]: The locationMatchingStrategy was specified as EXACT_LOCATION, but a locationType was also specified: lookups cannot limit by a location type when matching an exact location.'),
    ('values', 'probe', b'{"dataFilters": [{"developerMetadataLookup": {"locationType": "SHEET", "metadataLocation": {"spreadsheet": true}, "locationMatchingStrategy": "INTERSECTING_LOCATION"}}]}', 400, 'Invalid dataFilter[0]: DeveloperMetadataLookup.spreadsheet is true, but locationMatchingStrategy was specified as INTERSECTING.'),
    ('values', 'probe', b'{"dataFilters": [{"developerMetadataLookup": {"locationType": "SHEET", "metadataLocation": {"spreadsheet": true}, "locationMatchingStrategy": 9}}]}', 500, 'Internal error encountered.'),
    ('values', 'probe', b'{"dataFilters": [{"developerMetadataLookup": {"locationType": "SHEET", "metadataLocation": {"spreadsheet": false}}}]}', 500, 'Internal error encountered.'),
    ('values', 'probe', b'{"dataFilters": [{"developerMetadataLookup": {"locationType": "SHEET", "metadataLocation": {"spreadsheet": false}, "locationMatchingStrategy": "EXACT_LOCATION"}}]}', 400, 'Invalid dataFilter[0]: The locationMatchingStrategy was specified as EXACT_LOCATION, but a locationType was also specified: lookups cannot limit by a location type when matching an exact location.'),
    ('values', 'probe', b'{"dataFilters": [{"developerMetadataLookup": {"locationType": "SHEET", "metadataLocation": {"spreadsheet": false}, "locationMatchingStrategy": "INTERSECTING_LOCATION"}}]}', 500, 'Internal error encountered.'),
    ('values', 'probe', b'{"dataFilters": [{"developerMetadataLookup": {"locationType": "SHEET", "metadataLocation": {"spreadsheet": false}, "locationMatchingStrategy": 9}}]}', 500, 'Internal error encountered.'),
    ('values', 'probe', b'{"dataFilters": [{"developerMetadataLookup": {"locationType": "SHEET", "metadataLocation": {"sheetId": ID_DATA}}}]}', 200, None),
    ('values', 'probe', b'{"dataFilters": [{"developerMetadataLookup": {"locationType": "SHEET", "metadataLocation": {"sheetId": ID_DATA}, "locationMatchingStrategy": "EXACT_LOCATION"}}]}', 400, 'Invalid dataFilter[0]: The locationMatchingStrategy was specified as EXACT_LOCATION, but a locationType was also specified: lookups cannot limit by a location type when matching an exact location.'),
    ('values', 'probe', b'{"dataFilters": [{"developerMetadataLookup": {"locationType": "SHEET", "metadataLocation": {"sheetId": ID_DATA}, "locationMatchingStrategy": "INTERSECTING_LOCATION"}}]}', 200, None),
    ('values', 'probe', b'{"dataFilters": [{"developerMetadataLookup": {"locationType": "SHEET", "metadataLocation": {"sheetId": ID_DATA}, "locationMatchingStrategy": 9}}]}', 500, 'Internal error encountered.'),
    ('values', 'probe', b'{"dataFilters": [{"developerMetadataLookup": {"locationType": "SHEET", "metadataLocation": {"sheetId": 5}}}]}', 400, 'Invalid dataFilter[0]: No grid with id: 5'),
    ('values', 'probe', b'{"dataFilters": [{"developerMetadataLookup": {"locationType": "SHEET", "metadataLocation": {"sheetId": 5}, "locationMatchingStrategy": "EXACT_LOCATION"}}]}', 400, 'Invalid dataFilter[0]: The locationMatchingStrategy was specified as EXACT_LOCATION, but a locationType was also specified: lookups cannot limit by a location type when matching an exact location.'),
    ('values', 'probe', b'{"dataFilters": [{"developerMetadataLookup": {"locationType": "SHEET", "metadataLocation": {"sheetId": 5}, "locationMatchingStrategy": "INTERSECTING_LOCATION"}}]}', 400, 'Invalid dataFilter[0]: No grid with id: 5'),
    ('values', 'probe', b'{"dataFilters": [{"developerMetadataLookup": {"locationType": "SHEET", "metadataLocation": {"sheetId": 5}, "locationMatchingStrategy": 9}}]}', 400, 'Invalid dataFilter[0]: No grid with id: 5'),
    ('values', 'probe', b'{"dataFilters": [{"developerMetadataLookup": {"locationType": "SHEET", "metadataLocation": {"dimensionRange": {"sheetId": ID_DATA, "dimension": "ROWS", "startIndex": 0, "endIndex": 1}}}}]}', 200, None),
    ('values', 'probe', b'{"dataFilters": [{"developerMetadataLookup": {"locationType": "SHEET", "metadataLocation": {"dimensionRange": {"sheetId": ID_DATA, "dimension": "ROWS", "startIndex": 0, "endIndex": 1}}, "locationMatchingStrategy": "EXACT_LOCATION"}}]}', 400, 'Invalid dataFilter[0]: The locationMatchingStrategy was specified as EXACT_LOCATION, but a locationType was also specified: lookups cannot limit by a location type when matching an exact location.'),
    ('values', 'probe', b'{"dataFilters": [{"developerMetadataLookup": {"locationType": "SHEET", "metadataLocation": {"dimensionRange": {"sheetId": ID_DATA, "dimension": "ROWS", "startIndex": 0, "endIndex": 1}}, "locationMatchingStrategy": "INTERSECTING_LOCATION"}}]}', 200, None),
    ('values', 'probe', b'{"dataFilters": [{"developerMetadataLookup": {"locationType": "SHEET", "metadataLocation": {"dimensionRange": {"sheetId": ID_DATA, "dimension": "ROWS", "startIndex": 0, "endIndex": 1}}, "locationMatchingStrategy": 9}}]}', 500, 'Internal error encountered.'),
    ('values', 'probe', b'{"dataFilters": [{"developerMetadataLookup": {"locationType": "SHEET", "metadataLocation": {"dimensionRange": {"sheetId": ID_DATA, "dimension": "COLUMNS", "startIndex": 0, "endIndex": 1}}}}]}', 200, None),
    ('values', 'probe', b'{"dataFilters": [{"developerMetadataLookup": {"locationType": "SHEET", "metadataLocation": {"dimensionRange": {"sheetId": ID_DATA, "dimension": "COLUMNS", "startIndex": 0, "endIndex": 1}}, "locationMatchingStrategy": "EXACT_LOCATION"}}]}', 400, 'Invalid dataFilter[0]: The locationMatchingStrategy was specified as EXACT_LOCATION, but a locationType was also specified: lookups cannot limit by a location type when matching an exact location.'),
    ('values', 'probe', b'{"dataFilters": [{"developerMetadataLookup": {"locationType": "SHEET", "metadataLocation": {"dimensionRange": {"sheetId": ID_DATA, "dimension": "COLUMNS", "startIndex": 0, "endIndex": 1}}, "locationMatchingStrategy": "INTERSECTING_LOCATION"}}]}', 200, None),
    ('values', 'probe', b'{"dataFilters": [{"developerMetadataLookup": {"locationType": "SHEET", "metadataLocation": {"dimensionRange": {"sheetId": ID_DATA, "dimension": "COLUMNS", "startIndex": 0, "endIndex": 1}}, "locationMatchingStrategy": 9}}]}', 500, 'Internal error encountered.'),
    ('values', 'probe', b'{"dataFilters": [{"developerMetadataLookup": {"locationType": "SPREADSHEET"}}]}', 200, None),
    ('values', 'probe', b'{"dataFilters": [{"developerMetadataLookup": {"locationType": "SPREADSHEET", "locationMatchingStrategy": "EXACT_LOCATION"}}]}', 400, 'Invalid dataFilter[0]: The locationMatchingStrategy was specified as EXACT_LOCATION, but a locationType was also specified: lookups cannot limit by a location type when matching an exact location.'),
    ('values', 'probe', b'{"dataFilters": [{"developerMetadataLookup": {"locationType": "SPREADSHEET", "locationMatchingStrategy": "INTERSECTING_LOCATION"}}]}', 400, 'Invalid dataFilter[0]: A locationMatchingStrategy was specified, but no metadataLocation was specified: lookups must always specify a metadataLocation when specifying a locationMatchingStrategy.'),
    ('values', 'probe', b'{"dataFilters": [{"developerMetadataLookup": {"locationType": "SPREADSHEET", "locationMatchingStrategy": 9}}]}', 400, 'Invalid dataFilter[0]: A locationMatchingStrategy was specified, but no metadataLocation was specified: lookups must always specify a metadataLocation when specifying a locationMatchingStrategy.'),
    ('values', 'probe', b'{"dataFilters": [{"developerMetadataLookup": {"locationType": "SPREADSHEET", "metadataLocation": {}}}]}', 200, None),
    ('values', 'probe', b'{"dataFilters": [{"developerMetadataLookup": {"locationType": "SPREADSHEET", "metadataLocation": {}, "locationMatchingStrategy": "EXACT_LOCATION"}}]}', 400, 'Invalid dataFilter[0]: The locationMatchingStrategy was specified as EXACT_LOCATION, but a locationType was also specified: lookups cannot limit by a location type when matching an exact location.'),
    ('values', 'probe', b'{"dataFilters": [{"developerMetadataLookup": {"locationType": "SPREADSHEET", "metadataLocation": {}, "locationMatchingStrategy": "INTERSECTING_LOCATION"}}]}', 400, 'Invalid dataFilter[0]: A locationMatchingStrategy was specified, but no metadataLocation was specified: lookups must always specify a metadataLocation when specifying a locationMatchingStrategy.'),
    ('values', 'probe', b'{"dataFilters": [{"developerMetadataLookup": {"locationType": "SPREADSHEET", "metadataLocation": {}, "locationMatchingStrategy": 9}}]}', 400, 'Invalid dataFilter[0]: A locationMatchingStrategy was specified, but no metadataLocation was specified: lookups must always specify a metadataLocation when specifying a locationMatchingStrategy.'),
    ('values', 'probe', b'{"dataFilters": [{"developerMetadataLookup": {"locationType": "SPREADSHEET", "metadataLocation": {"spreadsheet": true}}}]}', 200, None),
    ('values', 'probe', b'{"dataFilters": [{"developerMetadataLookup": {"locationType": "SPREADSHEET", "metadataLocation": {"spreadsheet": true}, "locationMatchingStrategy": "EXACT_LOCATION"}}]}', 400, 'Invalid dataFilter[0]: The locationMatchingStrategy was specified as EXACT_LOCATION, but a locationType was also specified: lookups cannot limit by a location type when matching an exact location.'),
    ('values', 'probe', b'{"dataFilters": [{"developerMetadataLookup": {"locationType": "SPREADSHEET", "metadataLocation": {"spreadsheet": true}, "locationMatchingStrategy": "INTERSECTING_LOCATION"}}]}', 400, 'Invalid dataFilter[0]: DeveloperMetadataLookup.spreadsheet is true, but locationMatchingStrategy was specified as INTERSECTING.'),
    ('values', 'probe', b'{"dataFilters": [{"developerMetadataLookup": {"locationType": "SPREADSHEET", "metadataLocation": {"spreadsheet": true}, "locationMatchingStrategy": 9}}]}', 500, 'Internal error encountered.'),
    ('values', 'probe', b'{"dataFilters": [{"developerMetadataLookup": {"locationType": "SPREADSHEET", "metadataLocation": {"spreadsheet": false}}}]}', 400, 'Invalid dataFilter[0]: Cannot limit by location type of SPREADSHEET for the location spreadsheet'),
    ('values', 'probe', b'{"dataFilters": [{"developerMetadataLookup": {"locationType": "SPREADSHEET", "metadataLocation": {"spreadsheet": false}, "locationMatchingStrategy": "EXACT_LOCATION"}}]}', 400, 'Invalid dataFilter[0]: Cannot limit by location type of SPREADSHEET for the location spreadsheet'),
    ('values', 'probe', b'{"dataFilters": [{"developerMetadataLookup": {"locationType": "SPREADSHEET", "metadataLocation": {"spreadsheet": false}, "locationMatchingStrategy": "INTERSECTING_LOCATION"}}]}', 400, 'Invalid dataFilter[0]: Cannot limit by location type of SPREADSHEET for the location spreadsheet'),
    ('values', 'probe', b'{"dataFilters": [{"developerMetadataLookup": {"locationType": "SPREADSHEET", "metadataLocation": {"spreadsheet": false}, "locationMatchingStrategy": 9}}]}', 400, 'Invalid dataFilter[0]: Cannot limit by location type of SPREADSHEET for the location spreadsheet'),
    ('values', 'probe', b'{"dataFilters": [{"developerMetadataLookup": {"locationType": "SPREADSHEET", "metadataLocation": {"sheetId": ID_DATA}}}]}', 400, 'Invalid dataFilter[0]: Cannot limit by location type of SPREADSHEET for the location sheetId'),
    ('values', 'probe', b'{"dataFilters": [{"developerMetadataLookup": {"locationType": "SPREADSHEET", "metadataLocation": {"sheetId": ID_DATA}, "locationMatchingStrategy": "EXACT_LOCATION"}}]}', 400, 'Invalid dataFilter[0]: Cannot limit by location type of SPREADSHEET for the location sheetId'),
    ('values', 'probe', b'{"dataFilters": [{"developerMetadataLookup": {"locationType": "SPREADSHEET", "metadataLocation": {"sheetId": ID_DATA}, "locationMatchingStrategy": "INTERSECTING_LOCATION"}}]}', 400, 'Invalid dataFilter[0]: Cannot limit by location type of SPREADSHEET for the location sheetId'),
    ('values', 'probe', b'{"dataFilters": [{"developerMetadataLookup": {"locationType": "SPREADSHEET", "metadataLocation": {"sheetId": ID_DATA}, "locationMatchingStrategy": 9}}]}', 400, 'Invalid dataFilter[0]: Cannot limit by location type of SPREADSHEET for the location sheetId'),
    ('values', 'probe', b'{"dataFilters": [{"developerMetadataLookup": {"locationType": "SPREADSHEET", "metadataLocation": {"sheetId": 5}}}]}', 400, 'Invalid dataFilter[0]: Cannot limit by location type of SPREADSHEET for the location sheetId'),
    ('values', 'probe', b'{"dataFilters": [{"developerMetadataLookup": {"locationType": "SPREADSHEET", "metadataLocation": {"sheetId": 5}, "locationMatchingStrategy": "EXACT_LOCATION"}}]}', 400, 'Invalid dataFilter[0]: Cannot limit by location type of SPREADSHEET for the location sheetId'),
    ('values', 'probe', b'{"dataFilters": [{"developerMetadataLookup": {"locationType": "SPREADSHEET", "metadataLocation": {"sheetId": 5}, "locationMatchingStrategy": "INTERSECTING_LOCATION"}}]}', 400, 'Invalid dataFilter[0]: Cannot limit by location type of SPREADSHEET for the location sheetId'),
    ('values', 'probe', b'{"dataFilters": [{"developerMetadataLookup": {"locationType": "SPREADSHEET", "metadataLocation": {"sheetId": 5}, "locationMatchingStrategy": 9}}]}', 400, 'Invalid dataFilter[0]: Cannot limit by location type of SPREADSHEET for the location sheetId'),
    ('values', 'probe', b'{"dataFilters": [{"developerMetadataLookup": {"locationType": "SPREADSHEET", "metadataLocation": {"dimensionRange": {"sheetId": ID_DATA, "dimension": "ROWS", "startIndex": 0, "endIndex": 1}}}}]}', 400, 'Invalid dataFilter[0]: Cannot limit by location type of SPREADSHEET for the location dimensionRange'),
    ('values', 'probe', b'{"dataFilters": [{"developerMetadataLookup": {"locationType": "SPREADSHEET", "metadataLocation": {"dimensionRange": {"sheetId": ID_DATA, "dimension": "ROWS", "startIndex": 0, "endIndex": 1}}, "locationMatchingStrategy": "EXACT_LOCATION"}}]}', 400, 'Invalid dataFilter[0]: Cannot limit by location type of SPREADSHEET for the location dimensionRange'),
    ('values', 'probe', b'{"dataFilters": [{"developerMetadataLookup": {"locationType": "SPREADSHEET", "metadataLocation": {"dimensionRange": {"sheetId": ID_DATA, "dimension": "ROWS", "startIndex": 0, "endIndex": 1}}, "locationMatchingStrategy": "INTERSECTING_LOCATION"}}]}', 400, 'Invalid dataFilter[0]: Cannot limit by location type of SPREADSHEET for the location dimensionRange'),
    ('values', 'probe', b'{"dataFilters": [{"developerMetadataLookup": {"locationType": "SPREADSHEET", "metadataLocation": {"dimensionRange": {"sheetId": ID_DATA, "dimension": "ROWS", "startIndex": 0, "endIndex": 1}}, "locationMatchingStrategy": 9}}]}', 400, 'Invalid dataFilter[0]: Cannot limit by location type of SPREADSHEET for the location dimensionRange'),
    ('values', 'probe', b'{"dataFilters": [{"developerMetadataLookup": {"locationType": "SPREADSHEET", "metadataLocation": {"dimensionRange": {"sheetId": ID_DATA, "dimension": "COLUMNS", "startIndex": 0, "endIndex": 1}}}}]}', 400, 'Invalid dataFilter[0]: Cannot limit by location type of SPREADSHEET for the location dimensionRange'),
    ('values', 'probe', b'{"dataFilters": [{"developerMetadataLookup": {"locationType": "SPREADSHEET", "metadataLocation": {"dimensionRange": {"sheetId": ID_DATA, "dimension": "COLUMNS", "startIndex": 0, "endIndex": 1}}, "locationMatchingStrategy": "EXACT_LOCATION"}}]}', 400, 'Invalid dataFilter[0]: Cannot limit by location type of SPREADSHEET for the location dimensionRange'),
    ('values', 'probe', b'{"dataFilters": [{"developerMetadataLookup": {"locationType": "SPREADSHEET", "metadataLocation": {"dimensionRange": {"sheetId": ID_DATA, "dimension": "COLUMNS", "startIndex": 0, "endIndex": 1}}, "locationMatchingStrategy": "INTERSECTING_LOCATION"}}]}', 400, 'Invalid dataFilter[0]: Cannot limit by location type of SPREADSHEET for the location dimensionRange'),
    ('values', 'probe', b'{"dataFilters": [{"developerMetadataLookup": {"locationType": "SPREADSHEET", "metadataLocation": {"dimensionRange": {"sheetId": ID_DATA, "dimension": "COLUMNS", "startIndex": 0, "endIndex": 1}}, "locationMatchingStrategy": 9}}]}', 400, 'Invalid dataFilter[0]: Cannot limit by location type of SPREADSHEET for the location dimensionRange'),
    ('values', 'probe', b'{"dataFilters": [{"developerMetadataLookup": {"locationType": 9, "locationMatchingStrategy": "EXACT_LOCATION"}}]}', 400, 'Invalid dataFilter[0]: The locationMatchingStrategy was specified as EXACT_LOCATION, but a locationType was also specified: lookups cannot limit by a location type when matching an exact location.'),
    ('values', 'probe', b'{"dataFilters": [{"developerMetadataLookup": {"locationType": 9, "locationMatchingStrategy": "INTERSECTING_LOCATION"}}]}', 400, 'Invalid dataFilter[0]: A locationMatchingStrategy was specified, but no metadataLocation was specified: lookups must always specify a metadataLocation when specifying a locationMatchingStrategy.'),
    ('values', 'probe', b'{"dataFilters": [{"developerMetadataLookup": {"locationType": 9, "locationMatchingStrategy": 9}}]}', 400, 'Invalid dataFilter[0]: A locationMatchingStrategy was specified, but no metadataLocation was specified: lookups must always specify a metadataLocation when specifying a locationMatchingStrategy.'),
    ('values', 'probe', b'{"dataFilters": [{"developerMetadataLookup": {"locationType": 9, "metadataLocation": {}}}]}', 500, 'Internal error encountered.'),
    ('values', 'probe', b'{"dataFilters": [{"developerMetadataLookup": {"locationType": 9, "metadataLocation": {}, "locationMatchingStrategy": "EXACT_LOCATION"}}]}', 400, 'Invalid dataFilter[0]: The locationMatchingStrategy was specified as EXACT_LOCATION, but a locationType was also specified: lookups cannot limit by a location type when matching an exact location.'),
    ('values', 'probe', b'{"dataFilters": [{"developerMetadataLookup": {"locationType": 9, "metadataLocation": {}, "locationMatchingStrategy": "INTERSECTING_LOCATION"}}]}', 400, 'Invalid dataFilter[0]: A locationMatchingStrategy was specified, but no metadataLocation was specified: lookups must always specify a metadataLocation when specifying a locationMatchingStrategy.'),
    ('values', 'probe', b'{"dataFilters": [{"developerMetadataLookup": {"locationType": 9, "metadataLocation": {}, "locationMatchingStrategy": 9}}]}', 400, 'Invalid dataFilter[0]: A locationMatchingStrategy was specified, but no metadataLocation was specified: lookups must always specify a metadataLocation when specifying a locationMatchingStrategy.'),
    ('values', 'probe', b'{"dataFilters": [{"developerMetadataLookup": {"locationType": 9, "metadataLocation": {"spreadsheet": true}}}]}', 500, 'Internal error encountered.'),
    ('values', 'probe', b'{"dataFilters": [{"developerMetadataLookup": {"locationType": 9, "metadataLocation": {"spreadsheet": true}, "locationMatchingStrategy": "EXACT_LOCATION"}}]}', 400, 'Invalid dataFilter[0]: The locationMatchingStrategy was specified as EXACT_LOCATION, but a locationType was also specified: lookups cannot limit by a location type when matching an exact location.'),
    ('values', 'probe', b'{"dataFilters": [{"developerMetadataLookup": {"locationType": 9, "metadataLocation": {"spreadsheet": true}, "locationMatchingStrategy": "INTERSECTING_LOCATION"}}]}', 400, 'Invalid dataFilter[0]: DeveloperMetadataLookup.spreadsheet is true, but locationMatchingStrategy was specified as INTERSECTING.'),
    ('values', 'probe', b'{"dataFilters": [{"developerMetadataLookup": {"locationType": 9, "metadataLocation": {"spreadsheet": true}, "locationMatchingStrategy": 9}}]}', 500, 'Internal error encountered.'),
    ('values', 'probe', b'{"dataFilters": [{"developerMetadataLookup": {"locationType": 9, "metadataLocation": {"spreadsheet": false}}}]}', 500, 'Internal error encountered.'),
    ('values', 'probe', b'{"dataFilters": [{"developerMetadataLookup": {"locationType": 9, "metadataLocation": {"spreadsheet": false}, "locationMatchingStrategy": "EXACT_LOCATION"}}]}', 400, 'Invalid dataFilter[0]: The locationMatchingStrategy was specified as EXACT_LOCATION, but a locationType was also specified: lookups cannot limit by a location type when matching an exact location.'),
    ('values', 'probe', b'{"dataFilters": [{"developerMetadataLookup": {"locationType": 9, "metadataLocation": {"spreadsheet": false}, "locationMatchingStrategy": "INTERSECTING_LOCATION"}}]}', 500, 'Internal error encountered.'),
    ('values', 'probe', b'{"dataFilters": [{"developerMetadataLookup": {"locationType": 9, "metadataLocation": {"spreadsheet": false}, "locationMatchingStrategy": 9}}]}', 500, 'Internal error encountered.'),
    ('values', 'probe', b'{"dataFilters": [{"developerMetadataLookup": {"locationType": 9, "metadataLocation": {"sheetId": ID_DATA}}}]}', 500, 'Internal error encountered.'),
    ('values', 'probe', b'{"dataFilters": [{"developerMetadataLookup": {"locationType": 9, "metadataLocation": {"sheetId": ID_DATA}, "locationMatchingStrategy": "EXACT_LOCATION"}}]}', 400, 'Invalid dataFilter[0]: The locationMatchingStrategy was specified as EXACT_LOCATION, but a locationType was also specified: lookups cannot limit by a location type when matching an exact location.'),
    ('values', 'probe', b'{"dataFilters": [{"developerMetadataLookup": {"locationType": 9, "metadataLocation": {"sheetId": ID_DATA}, "locationMatchingStrategy": "INTERSECTING_LOCATION"}}]}', 500, 'Internal error encountered.'),
    ('values', 'probe', b'{"dataFilters": [{"developerMetadataLookup": {"locationType": 9, "metadataLocation": {"sheetId": ID_DATA}, "locationMatchingStrategy": 9}}]}', 500, 'Internal error encountered.'),
    ('values', 'probe', b'{"dataFilters": [{"developerMetadataLookup": {"locationType": 9, "metadataLocation": {"sheetId": 5}}}]}', 500, 'Internal error encountered.'),
    ('values', 'probe', b'{"dataFilters": [{"developerMetadataLookup": {"locationType": 9, "metadataLocation": {"sheetId": 5}, "locationMatchingStrategy": "EXACT_LOCATION"}}]}', 400, 'Invalid dataFilter[0]: The locationMatchingStrategy was specified as EXACT_LOCATION, but a locationType was also specified: lookups cannot limit by a location type when matching an exact location.'),
    ('values', 'probe', b'{"dataFilters": [{"developerMetadataLookup": {"locationType": 9, "metadataLocation": {"sheetId": 5}, "locationMatchingStrategy": "INTERSECTING_LOCATION"}}]}', 500, 'Internal error encountered.'),
    ('values', 'probe', b'{"dataFilters": [{"developerMetadataLookup": {"locationType": 9, "metadataLocation": {"sheetId": 5}, "locationMatchingStrategy": 9}}]}', 500, 'Internal error encountered.'),
    ('values', 'probe', b'{"dataFilters": [{"developerMetadataLookup": {"locationType": 9, "metadataLocation": {"dimensionRange": {"sheetId": ID_DATA, "dimension": "ROWS", "startIndex": 0, "endIndex": 1}}}}]}', 500, 'Internal error encountered.'),
    ('values', 'probe', b'{"dataFilters": [{"developerMetadataLookup": {"locationType": 9, "metadataLocation": {"dimensionRange": {"sheetId": ID_DATA, "dimension": "ROWS", "startIndex": 0, "endIndex": 1}}, "locationMatchingStrategy": "EXACT_LOCATION"}}]}', 400, 'Invalid dataFilter[0]: The locationMatchingStrategy was specified as EXACT_LOCATION, but a locationType was also specified: lookups cannot limit by a location type when matching an exact location.'),
    ('values', 'probe', b'{"dataFilters": [{"developerMetadataLookup": {"locationType": 9, "metadataLocation": {"dimensionRange": {"sheetId": ID_DATA, "dimension": "ROWS", "startIndex": 0, "endIndex": 1}}, "locationMatchingStrategy": "INTERSECTING_LOCATION"}}]}', 500, 'Internal error encountered.'),
    ('values', 'probe', b'{"dataFilters": [{"developerMetadataLookup": {"locationType": 9, "metadataLocation": {"dimensionRange": {"sheetId": ID_DATA, "dimension": "ROWS", "startIndex": 0, "endIndex": 1}}, "locationMatchingStrategy": 9}}]}', 500, 'Internal error encountered.'),
    ('values', 'probe', b'{"dataFilters": [{"developerMetadataLookup": {"locationType": 9, "metadataLocation": {"dimensionRange": {"sheetId": ID_DATA, "dimension": "COLUMNS", "startIndex": 0, "endIndex": 1}}}}]}', 500, 'Internal error encountered.'),
    ('values', 'probe', b'{"dataFilters": [{"developerMetadataLookup": {"locationType": 9, "metadataLocation": {"dimensionRange": {"sheetId": ID_DATA, "dimension": "COLUMNS", "startIndex": 0, "endIndex": 1}}, "locationMatchingStrategy": "EXACT_LOCATION"}}]}', 400, 'Invalid dataFilter[0]: The locationMatchingStrategy was specified as EXACT_LOCATION, but a locationType was also specified: lookups cannot limit by a location type when matching an exact location.'),
    ('values', 'probe', b'{"dataFilters": [{"developerMetadataLookup": {"locationType": 9, "metadataLocation": {"dimensionRange": {"sheetId": ID_DATA, "dimension": "COLUMNS", "startIndex": 0, "endIndex": 1}}, "locationMatchingStrategy": "INTERSECTING_LOCATION"}}]}', 500, 'Internal error encountered.'),
    ('values', 'probe', b'{"dataFilters": [{"developerMetadataLookup": {"locationType": 9, "metadataLocation": {"dimensionRange": {"sheetId": ID_DATA, "dimension": "COLUMNS", "startIndex": 0, "endIndex": 1}}, "locationMatchingStrategy": 9}}]}', 500, 'Internal error encountered.'),
    ('values', 'probe', b'{"dataFilters": [{"developerMetadataLookup": {"metadataLocation": {"dimensionRange": {"sheetId": ID_DATA, "startIndex": -1, "endIndex": 0}}}}]}', 400, 'Invalid dataFilter[0]: DimensionRange indexes must be >= 0'),
    ('values', 'probe', b'{"dataFilters": [{"developerMetadataLookup": {"metadataLocation": {"dimensionRange": {"sheetId": ID_DATA, "dimension": 7, "startIndex": -1, "endIndex": 0}}}}]}', 400, 'Invalid dataFilter[0]: DimensionRange indexes must be >= 0'),
    ('values', 'probe', b'{"dataFilters": [{"developerMetadataLookup": {"metadataLocation": {"dimensionRange": {"sheetId": 5, "startIndex": -1, "endIndex": 0}}}}]}', 400, 'Invalid dataFilter[0]: DimensionRange indexes must be >= 0'),
    ('values', 'probe', b'{"dataFilters": [{"developerMetadataLookup": {"metadataLocation": {"dimensionRange": {"sheetId": ID_DATA, "dimension": 7, "startIndex": 1000, "endIndex": 1001}}}}]}', 500, 'Internal error encountered.'),
    ('values', 'probe', b'{"dataFilters": [{"developerMetadataLookup": {"metadataLocation": {"dimensionRange": {"sheetId": ID_DATA, "startIndex": 1000, "endIndex": 1001}}}}]}', 400, 'Invalid dataFilter[0]: No dimension specified'),
    ('values', 'probe', b'{"dataFilters": [{"developerMetadataLookup": {"metadataLocation": {"dimensionRange": {"sheetId": ID_DATA, "dimension": "COLUMNS", "startIndex": -3, "endIndex": -2}}}}]}', 400, 'Invalid dataFilter[0]: DimensionRange indexes must be >= 0'),
    ('values', 'probe', b'{"dataFilters": [{"developerMetadataLookup": {"metadataLocation": {"dimensionRange": {"sheetId": ID_DATA, "startIndex": -5, "endIndex": -1}}}}]}', 400, 'Invalid dataFilter[0]: DimensionRange must represent a single row or column.'),
    ('values', 'probe', b'{"dataFilters": [{"developerMetadataLookup": {"metadataLocation": {"dimensionRange": {"sheetId": ID_DATA, "dimension": 7, "startIndex": 0, "endIndex": 2}}}}]}', 400, 'Invalid dataFilter[0]: DimensionRange must represent a single row or column.'),
    ('values', 'probe', b'{"dataFilters": [{"developerMetadataLookup": {"visibility": 9, "metadataLocation": {"sheetId": 5}}}]}', 400, 'Invalid dataFilter[0]: No grid with id: 5'),
    ('values', 'probe', b'{"dataFilters": [{"developerMetadataLookup": {"visibility": 9, "locationMatchingStrategy": "EXACT_LOCATION"}}]}', 400, 'Invalid dataFilter[0]: A locationMatchingStrategy was specified, but no metadataLocation was specified: lookups must always specify a metadataLocation when specifying a locationMatchingStrategy.'),
    ('values', 'probe', b'{"dataFilters": [{"developerMetadataLookup": {"metadataLocation": {"locationType": "ROW", "spreadsheet": true}}}]}', 200, None),
    ('values', 'probe', b'{"dataFilters": [{"developerMetadataLookup": {"metadataLocation": {"locationType": "SPREADSHEET", "sheetId": ID_DATA}}}]}', 200, None),
    ('values', 'probe', b'null', 400, 'Invalid JSON payload received. Unknown name "": Root element must be a message.'),
    ('sheet', 'probe', b'null', 400, 'Invalid JSON payload received. Unknown name "": Root element must be a message.'),
    ('values', 'probe', b'{"dataFilters": [{"gridRange": {"sheetId": ID_DATA, "startRowIndex": 1000, "endColumnIndex": 0}}]}', 400, 'Invalid dataFilter[0]: Range (Data!1001:) exceeds grid limits. Max rows: 1000, max columns: 26'),
    ('values', 'probe', b'{"dataFilters": [{"gridRange": {"sheetId": ID_DATA, "startColumnIndex": 26, "endRowIndex": 0}}]}', 400, 'Invalid dataFilter[0]: Range (Data!AA:0) exceeds grid limits. Max rows: 1000, max columns: 26'),
    ('values', 'probe', b'{"dataFilters": [{"gridRange": {"sheetId": ID_DATA, "endRowIndex": 0, "startColumnIndex": 26, "endColumnIndex": 27}}]}', 400, 'Invalid dataFilter[0]: Range (Data!AA:AA0) exceeds grid limits. Max rows: 1000, max columns: 26'),
    ('values', 'probe', b'{"dataFilters": [{"gridRange": {"sheetId": ID_DATA, "startRowIndex": 0, "endRowIndex": 0, "startColumnIndex": 26}}]}', 400, 'Invalid dataFilter[0]: Range ((empty) Data!AA1:0) exceeds grid limits. Max rows: 1000, max columns: 26'),
    ('values', 'probe', b'{"dataFilters": [{"gridRange": {"sheetId": ID_DATA, "startColumnIndex": 26, "endColumnIndex": 26, "startRowIndex": 1000}}]}', 400, 'Invalid dataFilter[0]: Range ((empty) Data!AA1001:Z) exceeds grid limits. Max rows: 1000, max columns: 26'),
    ('values', 'probe', b'{"dataFilters": [{"gridRange": {"sheetId": ID_DATA, "endColumnIndex": 18279}}]}', 200, [('Data!A1:Z1000', 'ROWS', {'gridRange': {'endColumnIndex': 18279, 'sheetId': 'ID_DATA'}})]),
    ('values', 'probe', b'{"dataFilters": [{"gridRange": {"sheetId": ID_DATA, "endColumnIndex": 20000}}]}', 200, [('Data!A1:Z1000', 'ROWS', {'gridRange': {'endColumnIndex': 20000, 'sheetId': 'ID_DATA'}})]),
    ('values', 'probe', b'{"dataFilters": [{"gridRange": {"sheetId": ID_DATA, "endColumnIndex": 2147483647}}]}', 200, [('Data!A1:Z1000', 'ROWS', {'gridRange': {'endColumnIndex': 2147483647, 'sheetId': 'ID_DATA'}})]),
    ('values', 'probe', b'{"dataFilters": [{"gridRange": {"sheetId": ID_DATA, "endRowIndex": 2147483647}}]}', 200, [('Data!A1:Z1000', 'ROWS', {'gridRange': {'endRowIndex': 2147483647, 'sheetId': 'ID_DATA'}})]),
    ('values', 'probe', b'{"dataFilters": [{"gridRange": {"sheetId": ID_DATA, "startRowIndex": 5, "endColumnIndex": 100000}}]}', 200, [('Data!A6:Z1000', 'ROWS', {'gridRange': {'endColumnIndex': 100000, 'sheetId': 'ID_DATA', 'startRowIndex': 5}})]),
    ('sheet', 'probe', b'{"dataFilters": [{"gridRange": {"sheetId": ID_DATA, "startRowIndex": 1000, "endColumnIndex": 0}}]}', 400, 'Range (Data!1001:) exceeds grid limits. Max rows: 1000, max columns: 26'),
    ('sheet', 'probe', b'{"dataFilters": [{"gridRange": {"sheetId": ID_DATA, "endColumnIndex": 20000}}], "includeGridData": false}', 200, [('Data', None)]),
    ('values', 'probe', b'{"dataFilters":[{"a1Range":"\\ud800"}]}', 400, 'Invalid dataFilter[0]: Unable to parse range: �'),
    ('values', 'probe', b'{"dataFilters":[{"a1Range":"\\udc00"}]}', 400, 'Invalid dataFilter[0]: Unable to parse range: �'),
    ('values', 'probe', b'{"dataFilters":[{"a1Range":"\\ud800A"}]}', 400, 'Invalid dataFilter[0]: Unable to parse range: �A'),
    ('values', 'probe', b'{"\\ud800x": 1}', 400, 'Invalid JSON payload received. Unknown name "�x": Cannot find field.'),
    ('values', 'probe', b'{"dataFilters":[{"a1Range":"\\ud800\\ud800"}]}', 400, 'Invalid dataFilter[0]: Unable to parse range: ��'),
    ('values', 'probe', b'{"dataFilters":[{"a1Range":"\\ud800\\u0041"}]}', 400, 'Invalid dataFilter[0]: Unable to parse range: �A'),
    ('values', 'probe', b'{"dataFilters": [{"a1Range": "Data!A1"}, [{"a1Range": 5}]]}', 400, "Invalid value at 'data_filters[0][0].a1_range' (TYPE_STRING), 5"),
    ('values', 'probe', b'{"dataFilters": [[{"a1Range": 5}]]}', 400, "Invalid value at 'data_filters[0].a1_range' (TYPE_STRING), 5"),
    ('values', 'probe', b'{"dataFilters": [[{"a1Range": "Data!A1"}, {"a1Range": 5}]]}', 400, "Invalid value at 'data_filters[1].a1_range' (TYPE_STRING), 5"),
    ('values', 'probe', b'{"dataFilters": [{"a1Range": "Data!A1"}, {"a1Range": "Data!A2"}, [{"a1Range": 5}]]}', 400, "Invalid value at 'data_filters[1][0].a1_range' (TYPE_STRING), 5"),
    ('values', 'probe', b'{"dataFilters": [[{"a1Range": "Data!A1"}], {"a1Range": 5}]}', 400, "Invalid value at 'data_filters[0].a1_range' (TYPE_STRING), 5"),
    ('values', 'probe', b'{"dataFilters": [[], {"a1Range": 5}]}', 400, "Invalid value at 'data_filters[0].a1_range' (TYPE_STRING), 5"),
    ('values', 'probe', b'{"dataFilters": [{"a1Range": "Data!A1"}, ["abc"]]}', 400, 'Invalid value at \'data_filters[0][0]\' (type.googleapis.com/google.apps.sheets.v4.DataFilter), "abc"'),
    ('values', 'probe', b'{"dataFilters": [{"a1Range": "Data!A1"}, [[{"a1Range": 5}]]]}', 400, "Invalid value at 'data_filters[0][0].a1_range' (TYPE_STRING), 5"),
    ('values', 'probe', b'{"dataFilters": [{"a1Range": "Data!A1"}, [{"a1Range": "Data!A1"}, {"a1Range": 5}]]}', 400, "Invalid value at 'data_filters[0][1].a1_range' (TYPE_STRING), 5"),
    ('sheet', 'probe', b'{"dataFilters": [{"a1Range": "Data!A1"}], "includeGridData": 1}', 200, [('Data', [{'columnMetadata': 1, 'rowMetadata': 1}])]),
    ('sheet', 'probe', b'{"dataFilters": [{"a1Range": "Data!A1"}], "includeGridData": 1.0}', 200, [('Data', [{'columnMetadata': 1, 'rowMetadata': 1}])]),
    ('sheet', 'probe', b'{"dataFilters": [{"a1Range": "Data!A1"}], "includeGridData": 0}', 200, [('Data', None)]),
    ('values', 'probe', b'{"dataFilters": [{"gridRange": {"sheetId": ID_DATA, "startRowIndex": 1, "endRowIndex": 1}}, {"gridRange": {"sheetId": ID_DATA, "startRowIndex": 2, "endRowIndex": 2}}]}', 200, [('#REF!', 'ROWS', {'gridRange': {'endRowIndex': 1, 'sheetId': 'ID_DATA', 'startRowIndex': 1}}, {'gridRange': {'endRowIndex': 2, 'sheetId': 'ID_DATA', 'startRowIndex': 2}})]),
    ('values', 'probe', b'{"dataFilters": [{"gridRange": {"sheetId": ID_DATA, "startRowIndex": 1, "endRowIndex": 1}}, {"gridRange": {"sheetId": ID_DATA, "startRowIndex": 1, "endRowIndex": 1}}]}', 200, [('#REF!', 'ROWS', {'gridRange': {'endRowIndex': 1, 'sheetId': 'ID_DATA', 'startRowIndex': 1}}, {'gridRange': {'endRowIndex': 1, 'sheetId': 'ID_DATA', 'startRowIndex': 1}})]),
    ('values', 'probe', b'{"dataFilters": [{"gridRange": {"sheetId": ID_DATA, "startRowIndex": 1, "endRowIndex": 1}}, {"gridRange": {"sheetId": ID_DATA, "startColumnIndex": 1, "endColumnIndex": 1}}]}', 200, [('#REF!', 'ROWS', {'gridRange': {'endRowIndex': 1, 'sheetId': 'ID_DATA', 'startRowIndex': 1}}, {'gridRange': {'endColumnIndex': 1, 'sheetId': 'ID_DATA', 'startColumnIndex': 1}})]),
    ('values', 'probe', b'{"dataFilters": [{"a1Range": "Data!A1:A1"}, {"a1Range": "Data!A1"}]}', 200, [('Data!A1', 'ROWS', {'a1Range': 'Data!A1:A1'}, {'a1Range': 'Data!A1'})]),
    ('values', 'probe', b'{"dataFilters": [{"a1Range": "data!A1"}, {"a1Range": "Data!A1"}]}', 200, [('Data!A1', 'ROWS', {'a1Range': 'data!A1'}, {'a1Range': 'Data!A1'})]),
    ('values', 'probe', b'{"dataFilters": [{"a1Range": "Data"}, {"a1Range": "Data!A1:Z1000"}]}', 200, [('Data!A1:Z1000', 'ROWS', {'a1Range': 'Data'}, {'a1Range': 'Data!A1:Z1000'})]),
    ('values', 'probe', b'{"dataFilters": [{"a1Range": "Data!A1:B2"}, {"gridRange": {"sheetId": ID_DATA, "startRowIndex": 0, "endRowIndex": 2, "startColumnIndex": 0, "endColumnIndex": 2}}]}', 200, [('Data!A1:B2', 'ROWS', {'a1Range': 'Data!A1:B2'}, {'gridRange': {'endColumnIndex': 2, 'endRowIndex': 2, 'sheetId': 'ID_DATA', 'startColumnIndex': 0, 'startRowIndex': 0}})]),
    ('sheet', 'probe', b'{"dataFilters": [{"a1Range": "Data!A1:A1"}, {"a1Range": "Data!A1"}], "includeGridData": true}', 200, [('Data', [{'columnMetadata': 1, 'rowMetadata': 1}])]),
    ('sheet', 'probe', b'{"dataFilters": [{"a1Range": "data!A1"}, {"a1Range": "Data!A1"}], "includeGridData": true}', 200, [('Data', [{'columnMetadata': 1, 'rowMetadata': 1}])]),
    ('sheet', 'probe', b'{"dataFilters": [{"a1Range": "Data!A1"}, {"a1Range": "Data!B1"}, {"a1Range": "Data!A1"}], "includeGridData": true}', 200, [('Data', [{'columnMetadata': 1, 'rowMetadata': 1}, {'columnMetadata': 1, 'rowMetadata': 1, 'startColumn': 1}])]),
    ('sheet', 'probe', b'{"dataFilters": [{"a1Range": "Data!A1:B2"}, {"gridRange": {"sheetId": ID_DATA, "startRowIndex": 0, "endRowIndex": 2, "startColumnIndex": 0, "endColumnIndex": 2}}], "includeGridData": true}', 200, [('Data', [{'columnMetadata': 2, 'rowMetadata': 2}])]),
    ('sheet', 'probe', b'{"dataFilters": [{"gridRange": {"sheetId": ID_DATA, "startRowIndex": 1, "endRowIndex": 1}}, {"gridRange": {"sheetId": ID_DATA, "startRowIndex": 2, "endRowIndex": 2}}], "includeGridData": true}', 200, [('Data', [{'columnMetadata': 26, 'startRow': 1}, {'columnMetadata': 26, 'startRow': 2}])]),
    ('values', 'probe', b'{"dataFilters": [{"developerMetadataLookup": {"metadataLocation": {"dimensionRange": {"sheetId": ID_DATA, "dimension": "ROWS", "startIndex": 2147483647, "endIndex": -2147483648}}}}]}', 400, 'Invalid dataFilter[0]: DimensionRange indexes must be >= 0'),
    ('sheet', 'probe', b'{"dataFilters": [{"gridRange": {"sheetId": ID_DATA, "startRowIndex": 1, "endRowIndex": 1}}, {"gridRange": {"sheetId": ID_DATA, "startRowIndex": 1, "endRowIndex": 1}}], "includeGridData": true}', 200, [('Data', [{'columnMetadata': 26, 'startRow': 1}])]),
    ('sheet', 'probe', b'{"dataFilters": [{"gridRange": {"sheetId": ID_DATA, "startRowIndex": 1, "endRowIndex": 1}}, {"gridRange": {"sheetId": ID_DATA, "startColumnIndex": 1, "endColumnIndex": 1}}], "includeGridData": true}', 200, [('Data', [{'columnMetadata': 26, 'startRow': 1}, {'rowMetadata': 1000, 'startColumn': 1}])]),
    ('sheet', 'probe', b'{"dataFilters": [{"a1Range": "Data"}, {"a1Range": "Data!A1:Z1000"}], "includeGridData": true}', 200, [('Data', [{'columnMetadata': 26, 'rowMetadata': 1000}])]),
    ('sheet', 'probe', b'{"dataFilters": [{"a1Range": "Data!A1:A2000"}, {"a1Range": "Data!A1:A1000"}], "includeGridData": true}', 200, [('Data', [{'columnMetadata': 1, 'rowMetadata': 1000}])]),
    ('sheet', 'probe', b'{"dataFilters": [{"gridRange": {"sheetId": ID_DATA, "startRowIndex": 0, "endRowIndex": 1, "startColumnIndex": 0, "endColumnIndex": 100}}, {"gridRange": {"sheetId": ID_DATA, "startRowIndex": 0, "endRowIndex": 1, "startColumnIndex": 0, "endColumnIndex": 26}}], "includeGridData": true}', 200, [('Data', [{'columnMetadata': 26, 'rowMetadata': 1}])]),
    ('sheet', 'probe', b'{"dataFilters": [{"a1Range": "Sheet1!A1"}, {"a1Range": "Data!A1"}], "includeGridData": true}', 200, [('Sheet1', [{'columnMetadata': 1, 'rowMetadata': 1}]), ('Data', [{'columnMetadata': 1, 'rowMetadata': 1}])]),
    ('sheet', 'probe', b'{"dataFilters": [{"a1Range": "Data!B1"}, {"a1Range": "Data!A1"}, {"a1Range": "Data!B1"}], "includeGridData": true}', 200, [('Data', [{'columnMetadata': 1, 'rowMetadata': 1, 'startColumn': 1}, {'columnMetadata': 1, 'rowMetadata': 1}])]),
    ('sheet', 'probe', b'{"dataFilters": [{"a1Range": "Data!A2"}, {"a1Range": "Data!A1"}], "includeGridData": true}', 200, [('Data', [{'columnMetadata': 1, 'rowMetadata': 1, 'startRow': 1}, {'columnMetadata': 1, 'rowMetadata': 1}])]),
    ('sheet', 'probe', b'{"dataFilters": [{"gridRange": {"sheetId": ID_DATA, "startRowIndex": 1, "endRowIndex": 1}}, {"a1Range": "Data!A1"}, {"gridRange": {"sheetId": ID_DATA, "startRowIndex": 1, "endRowIndex": 1}}], "includeGridData": true}', 200, [('Data', [{'columnMetadata': 26, 'startRow': 1}, {'columnMetadata': 1, 'rowMetadata': 1}])]),
    ('values', 'probe', b'{"dataFilters": [{"a1Range": "Data!A1:A2000"}, {"a1Range": "Data!A1:A1000"}]}', 200, [('Data!A1:A1000', 'ROWS', {'a1Range': 'Data!A1:A2000'}, {'a1Range': 'Data!A1:A1000'})]),
    ('values', 'probe', b'{"dataFilters": [{"gridRange": {"sheetId": ID_DATA, "startRowIndex": 0, "endRowIndex": 1, "startColumnIndex": 0, "endColumnIndex": 100}}, {"gridRange": {"sheetId": ID_DATA, "startRowIndex": 0, "endRowIndex": 1, "startColumnIndex": 0, "endColumnIndex": 26}}]}', 200, [('Data!A1:Z1', 'ROWS', {'gridRange': {'endColumnIndex': 100, 'endRowIndex': 1, 'sheetId': 'ID_DATA', 'startColumnIndex': 0, 'startRowIndex': 0}}, {'gridRange': {'endColumnIndex': 26, 'endRowIndex': 1, 'sheetId': 'ID_DATA', 'startColumnIndex': 0, 'startRowIndex': 0}})]),
    ('values', 'probe', b'{"dataFilters": [{"a1Range": "Sheet1!A1"}, {"a1Range": "Data!A1"}]}', 200, [('Sheet1!A1', 'ROWS', {'a1Range': 'Sheet1!A1'}), ('Data!A1', 'ROWS', {'a1Range': 'Data!A1'})]),
    ('values', 'probe', b'{"dataFilters": [{"a1Range": "Data!B1"}, {"a1Range": "Data!A1"}, {"a1Range": "Data!B1"}]}', 200, [('Data!A1', 'ROWS', {'a1Range': 'Data!A1'}), ('Data!B1', 'ROWS', {'a1Range': 'Data!B1'}, {'a1Range': 'Data!B1'})]),
    ('values', 'probe', b'{"dataFilters": [{"a1Range": "Data!A2"}, {"a1Range": "Data!A1"}]}', 200, [('Data!A1', 'ROWS', {'a1Range': 'Data!A1'}), ('Data!A2', 'ROWS', {'a1Range': 'Data!A2'})]),
    ('sheet', 'probe', b'{"dataFilters": [{"gridRange": {"sheetId": ID_DATA, "startRowIndex": 1, "endRowIndex": 1, "startColumnIndex": 0, "endColumnIndex": 100}}, {"gridRange": {"sheetId": ID_DATA, "startRowIndex": 1, "endRowIndex": 1}}], "includeGridData": true}', 200, [('Data', [{'columnMetadata': 26, 'startRow': 1}])]),
    ('sheet', 'probe', b'{"dataFilters": [{"gridRange": {"sheetId": ID_DATA, "startRowIndex": 1, "endRowIndex": 1, "startColumnIndex": 0, "endColumnIndex": 5}}, {"gridRange": {"sheetId": ID_DATA, "startRowIndex": 1, "endRowIndex": 1}}], "includeGridData": true}', 200, [('Data', [{'columnMetadata': 5, 'startRow': 1}, {'columnMetadata': 26, 'startRow': 1}])]),
    ('values', 'probe', b'{"dataFilters": [{"a1Range": "Data!A1"}], "": "x"}', 400, 'Invalid JSON payload received. Unknown name "": Proto fields must have a name.'),
    ('values', 'probe', b'{"dataFilters": [{"a1Range": "Data!A1"}], "": {"a": 1}}', 400, 'Invalid JSON payload received. Unknown name "": Proto fields must have a name.'),
    ('values', 'probe', b'{"dataFilters": [{"a1Range": "Data!A1"}], "": [1]}', 400, 'Invalid JSON payload received. Unknown name "": Proto fields must have a name.'),
    ('values', 'probe', b'{"dataFilters": [{"a1Range": "Data!A1"}], "": null}', 400, 'Invalid JSON payload received. Unknown name "": Proto fields must have a name.'),
    ('values', 'probe', b'{"dataFilters": [{"a1Range": "Data!A1"}], "": {}}', 400, 'Invalid JSON payload received. Unknown name "": Proto fields must have a name.'),
    ('values', 'probe', b'{"dataFilters": [{"a1Range": "Data!A1"}], "": 1}', 400, 'Invalid JSON payload received. Unknown name "": Proto fields must have a name.'),
    ('values', 'probe', b'{"dataFilters": [{"gridRange": {"sheetId": ID_DATA, "": 1}}]}', 400, 'Invalid JSON payload received. Unknown name "" at \'data_filters[0].grid_range\': Proto fields must have a name.'),
    ('values', 'probe', b'{"dataFilters": [{"gridRange": {"sheetId": ID_DATA, "": {}}}]}', 400, 'Invalid JSON payload received. Unknown name "" at \'data_filters[0].grid_range\': Proto fields must have a name.'),
    ('values', 'probe', b'{"dataFilters": [{"gridRange": {"sheetId": ID_DATA, "": null}}]}', 400, 'Invalid JSON payload received. Unknown name "" at \'data_filters[0].grid_range\': Proto fields must have a name.'),
    ('values', 'probe', b'{"dataFilters": [{"developerMetadataLookup": {"": 1}}]}', 400, 'Invalid JSON payload received. Unknown name "" at \'data_filters[0].developer_metadata_lookup\': Proto fields must have a name.'),
    ('values', 'probe', b'{"dataFilters": [{"developerMetadataLookup": {"metadataLocation": {"": 1}}}]}', 400, 'Invalid JSON payload received. Unknown name "" at \'data_filters[0].developer_metadata_lookup.metadata_location\': Proto fields must have a name.'),
    ('values', 'probe', b'{"dataFilters": [{"": 1}]}', 400, "Invalid value at 'data_filters[0]' (type.googleapis.com/google.apps.sheets.v4.DataFilter), 1"),
    ('values', 'probe', b'{"dataFilters": [{"": "x"}]}', 400, 'Invalid value at \'data_filters[0]\' (type.googleapis.com/google.apps.sheets.v4.DataFilter), "x"'),
    ('values', 'probe', b'{"dataFilters": [{"": null}]}', 400, 'Invalid dataFilter[0]: dataFilter.filter must be specified.'),
    ('values', 'probe', b'{"dataFilters": [{"": []}]}', 400, 'Invalid dataFilter[0]: dataFilter.filter must be specified.'),
    ('values', 'probe', b'{"dataFilters": [{"a1Range": "Data!A1", "": 1}]}', 400, "Invalid value at 'data_filters[0]' (type.googleapis.com/google.apps.sheets.v4.DataFilter), 1"),
    ('values', 'probe', b'{"dataFilters": [{"": 1, "a1Range": "Data!A1"}]}', 400, "Invalid value at 'data_filters[0]' (type.googleapis.com/google.apps.sheets.v4.DataFilter), 1"),
    ('values', 'probe', b'{"dataFilters": [{"": 1}, {"": 2}]}', 400, "Invalid value at 'data_filters[0]' (type.googleapis.com/google.apps.sheets.v4.DataFilter), 1\nInvalid value at 'data_filters[1]' (type.googleapis.com/google.apps.sheets.v4.DataFilter), 2"),
    ('values', 'probe', b'{"dataFilters": [[{"": 1}]]}', 400, "Invalid value at 'data_filters[0]' (type.googleapis.com/google.apps.sheets.v4.DataFilter), 1"),
    ('values', 'probe', b'{"dataFilters": [{"a1Range": "Data!A1"}, {"": 1}]}', 400, "Invalid value at 'data_filters[1]' (type.googleapis.com/google.apps.sheets.v4.DataFilter), 1"),
    ('values', 'probe', b'{"dataFilters": [{"gridRange": {"sheetId": ID_DATA, "startColumnIndex": 18277}}]}', 400, 'Invalid dataFilter[0]: Range (Data!ZZZ:) exceeds grid limits. Max rows: 1000, max columns: 26'),
    ('values', 'probe', b'{"dataFilters": [{"gridRange": {"sheetId": ID_DATA, "startColumnIndex": 18278}}]}', 400, 'Invalid dataFilter[0]: Range (Data!) exceeds grid limits. Max rows: 1000, max columns: 26'),
    ('values', 'probe', b'{"dataFilters": [{"gridRange": {"sheetId": ID_DATA, "startColumnIndex": 26, "endColumnIndex": 18279}}]}', 400, 'Invalid dataFilter[0]: Range (Data!AA:) exceeds grid limits. Max rows: 1000, max columns: 26'),
    ('values', 'probe', b'{"dataFilters": [{"gridRange": {"sheetId": ID_DATA, "startColumnIndex": 26, "endColumnIndex": 18278}}]}', 400, 'Invalid dataFilter[0]: Range (Data!AA:ZZZ) exceeds grid limits. Max rows: 1000, max columns: 26'),
    ('values', 'probe', b'{"dataFilters": [{"gridRange": {"sheetId": ID_DATA, "startRowIndex": 0, "endRowIndex": 2, "startColumnIndex": 18278, "endColumnIndex": 18279}}]}', 400, 'Invalid dataFilter[0]: Range (Data!1:2) exceeds grid limits. Max rows: 1000, max columns: 26'),
    ('values', 'probe', b'{"dataFilters": [{"gridRange": {"sheetId": ID_DATA, "startColumnIndex": 18278, "endColumnIndex": 18278}}]}', 400, 'Invalid dataFilter[0]: Range ((empty) Data!:ZZZ) exceeds grid limits. Max rows: 1000, max columns: 26'),
    ('values', 'probe', b'{"dataFilters": [{"gridRange": {"sheetId": ID_DATA, "startColumnIndex": 2147483647}}]}', 400, 'Invalid dataFilter[0]: Range (Data!) exceeds grid limits. Max rows: 1000, max columns: 26'),
    ('values', 'probe', b'{"dataFilters": [{"gridRange": {"sheetId": ID_DATA, "startRowIndex": 4, "startColumnIndex": 100000, "endColumnIndex": 100001}}]}', 400, 'Invalid dataFilter[0]: Range (Data!5:) exceeds grid limits. Max rows: 1000, max columns: 26'),
    ('sheet', 'probe', b'{"dataFilters": [{"a1Range": "Data!A1"}], "": 1}', 400, 'Invalid JSON payload received. Unknown name "": Proto fields must have a name.'),
    ('sheet', 'probe', b'{"dataFilters": [{"": 1}]}', 400, "Invalid value at 'data_filters[0]' (type.googleapis.com/google.apps.sheets.v4.DataFilter), 1"),
    ('sheet', 'probe', b'{"dataFilters": [{"gridRange": {"sheetId": ID_DATA, "startColumnIndex": 18278}}]}', 400, 'Range (Data!) exceeds grid limits. Max rows: 1000, max columns: 26'),
    ('sheet', 'probe', b'{"dataFilters": [{"gridRange": {"sheetId": ID_DATA, "startColumnIndex": 1, "endColumnIndex": 1, "endRowIndex": 2000}}], "includeGridData": true}', 200, [('Data', [{'rowMetadata': 1000, 'startColumn': 1}])]),
    ('sheet', 'probe', b'{"dataFilters": [{"gridRange": {"sheetId": ID_DATA, "startRowIndex": 3, "startColumnIndex": 1, "endColumnIndex": 1, "endRowIndex": 2000}}], "includeGridData": true}', 200, [('Data', [{'rowMetadata': 997, 'startColumn': 1, 'startRow': 3}])]),
    ('values', 'probe', b'{"dataFilters": [{"": {"bogus": 1}}]}', 400, 'Invalid JSON payload received. Unknown name "bogus" at \'data_filters[0]\': Cannot find field.'),
    ('values', 'probe', b'{"dataFilters": [{"": {"a1Range": 5}}]}', 400, "Invalid value at 'data_filters[0].a1_range' (TYPE_STRING), 5"),
    ('values', 'probe', b'{"dataFilters": [{"a1Range": "Data!A1"}], "": 1}', 400, ('Invalid JSON payload received. Unknown name "": Proto fields must have a name.', 'invalid', None)),
    ('values', 'probe', b'{"dataFilters": [{"": 1}]}', 400, ("Invalid value at 'data_filters[0]' (type.googleapis.com/google.apps.sheets.v4.DataFilter), 1", 'invalid', None)),
    # measured 2026-10-05
    ('values', 'probe', b'{"dataFilters": [{"a1Range": "Data!A1:B2"}], "valueRenderOption": -1}', 400, 'Invalid valueRenderOption: UNRECOGNIZED'),
    ('values', 'probe', b'{"dataFilters": [{"a1Range": "Data!A1:B2"}], "majorDimension": -1}', 500, 'Internal error encountered.'),
    ('values', 'probe', b'{"dataFilters": [{"a1Range": "Data!A1:B2"}], "dateTimeRenderOption": -1}', 200, [('Data!A1:B2', 'ROWS', {'a1Range': 'Data!A1:B2'})]),
    ('values', 'probe', b'{"dataFilters": [{"a1Range": "Data!A1:B2"}], "majorDimension": "row\xc5\xbf"}', 400, 'Invalid value at \'major_dimension\' (type.googleapis.com/google.apps.sheets.v4.Dimension), "rowſ"'),
    ('values', 'probe', b'{"dataFilters": [{"a1Range": "Data!A1:B2"}], "valueRenderOption": "formula\xc5\xbf"}', 400, 'Invalid value at \'value_render_option\' (type.googleapis.com/google.apps.sheets.v4.ValueRenderOption), "formulaſ"'),
    ('sheet', 'probe', b'{"dataFilters": [{"a1Range": "Data!A1"}], "includeGridData": "ye\xc5\xbf"}', 400, 'Invalid value at \'include_grid_data\' (TYPE_BOOL), "yeſ"'),
    ('values', 'probe', b'{"dataFilters": [{"developerMetadataLookup": {"locationType": -1}}]}', 500, 'Internal error encountered.'),
    ('sheet', 'probe', b'{"dataFilters": [{"developerMetadataLookup": {"locationType": -1}}]}', 500, 'Internal error encountered.'),
    ('values', 'probe', b'{"dataFilters": [{"developerMetadataLookup": {"metadataLocation": {"sheetId": ID_DATA}, "locationMatchingStrategy": -1}}]}', 500, 'Internal error encountered.'),
    ('sheet', 'probe', b'{"dataFilters": [{"developerMetadataLookup": {"metadataLocation": {"sheetId": ID_DATA}, "locationMatchingStrategy": -1}}]}', 500, 'Internal error encountered.'),
    ('values', 'probe', b'{"dataFilters": [{"developerMetadataLookup": {"visibility": -1}}]}', 500, 'Internal error encountered.'),
    ('sheet', 'probe', b'{"dataFilters": [{"developerMetadataLookup": {"visibility": -1}}]}', 500, 'Internal error encountered.'),
    ('values', 'probe', b'{"dataFilters": [{"developerMetadataLookup": {"metadataLocation": {"dimensionRange": {"sheetId": ID_DATA, "dimension": -1, "startIndex": 0, "endIndex": 1}}}}]}', 500, 'Internal error encountered.'),
    ('sheet', 'probe', b'{"dataFilters": [{"developerMetadataLookup": {"metadataLocation": {"dimensionRange": {"sheetId": ID_DATA, "dimension": -1, "startIndex": 0, "endIndex": 1}}}}]}', 500, 'Internal error encountered.'),
    # measured 2026-10-06
    ('values', 'probe', b'{"dataFilters":[{"a1Range":"\xff"}]}', 400, 'Invalid dataFilter[0]: Unable to parse range:  '),
    ('values', 'probe', b'{"dataFilters":[{"a1Range":"a\xffb"}]}', 400, 'Invalid dataFilter[0]: Unable to parse range: a b'),
    ('values', 'probe', b'{"dataFilters":[{"a1Range":"a\xe2\x82b"}]}', 400, 'Invalid dataFilter[0]: Unable to parse range: a  b'),
    ('values', 'probe', b'{"dataFilters":[{"a1Range":"a\xed\xa0\x80b"}]}', 400, 'Invalid dataFilter[0]: Unable to parse range: a   b'),
    ('values', 'probe', b'{"dataFilters":[{"a1Range":"a\x01b"}]}', 400, 'Invalid dataFilter[0]: Unable to parse range: a\x01b'),
    ('values', 'probe', b'{"dataFilters":[{"a1Range":"a\tb"}]}', 400, 'Invalid dataFilter[0]: Unable to parse range: a\tb'),
    ('values', 'probe', b'{"a":"\xff"}', 400, 'Invalid JSON payload received. Unknown name "a": Cannot find field.'),
    ('values', 'probe', b'{"a":"\x01"}', 400, 'Invalid JSON payload received. Unknown name "a": Cannot find field.'),
    ('values', 'probe', b'{"a\xff":1}', 400, 'Invalid JSON payload received. Unknown name "a ": Cannot find field.'),
    ('sheet', 'probe', b'{"dataFilters":[{"a1Range":"a\xffb"}]}', 400, 'Unable to parse range: a b'),
    ('values', 'probe', b'{"dataFilters": [{"gridRange": {"sheetId": ID_SHEET1, "startRowIndex": -9223372036854775808}}]}', 400, "Invalid value at 'data_filters[0].grid_range.start_row_index.value' (TYPE_INT32), -9223372036854775808"),
    ('values', 'probe', b'{"dataFilters": [{"gridRange": {"sheetId": ID_SHEET1, "startRowIndex": -9223372036854775809}}]}', 400, "Invalid value at 'data_filters[0].grid_range.start_row_index.value' (TYPE_INT32), -9.2233720368547758e+18"),
    ('values', 'probe', b'{"dataFilters": [{"gridRange": {"sheetId": ID_SHEET1, "startRowIndex": 18446744073709551615}}]}', 400, "Invalid value at 'data_filters[0].grid_range.start_row_index.value' (TYPE_INT32), 18446744073709551615"),
    ('values', 'probe', b'{"dataFilters": [{"gridRange": {"sheetId": ID_SHEET1, "startRowIndex": 18446744073709551616}}]}', 400, "Invalid value at 'data_filters[0].grid_range.start_row_index.value' (TYPE_INT32), 1.8446744073709552e+19"),
    ('values', 'probe', b'{"dataFilters": [{"gridRange": {"sheetId": ID_SHEET1, "startRowIndex": 9223372036854775808}}]}', 400, "Invalid value at 'data_filters[0].grid_range.start_row_index.value' (TYPE_INT32), 9223372036854775808"),
]
# fmt: on


def _by_filter_shown(route: str, r, xgafv: bool):
    """What a `MEASURED_BY_FILTER` row's ``shown`` holds, read off a Backlot answer."""
    body = r.json()
    if r.status_code != 200:
        err = body["error"]
        if not xgafv:
            return err["message"]
        return (err["message"], err["errors"][0].get("reason"), err["errors"][0].get("domain"))
    if route == "values":
        if "valueRanges" not in body:
            return None
        return [
            (v["valueRange"]["range"], v["valueRange"]["majorDimension"], *v["dataFilters"])
            for v in body["valueRanges"]
        ]
    return [
        (
            s["properties"]["title"],
            [
                {k: len(v) if isinstance(v, list) else v for k, v in d.items() if k != "rowData"}
                for d in s["data"]
            ]
            if "data" in s
            else None,
        )
        for s in body["sheets"]
    ]


def test_the_data_filter_reads_answer_every_measured_request(tmp_path):
    """All `MEASURED_BY_FILTER` rows over a corpus with the probe's five sheets, one test for the
    reason `test_sheets_values_answer_every_measured_r1c1_request_as_real_does` is one.

    A refusal of a value or a name also carries a field violation per line of its message, naming
    the location the line quotes, or none at the root of the body (`gerr.invalid_field_values`)."""
    from tests._helpers import build_corpus, client_for

    grid = [["a", "b"], ["c", "d"], ["e", "f"]]
    record = {
        "source_type": "google_drive",
        "doc_id": "probe",
        "folder": "mk",
        "title": "backlot #174 R1C1 probe",
        "author_email": "a@x.com",
        "visibility": "public",
        "subtype": "spreadsheet",
        "sheets": [
            {"title": t, "grid": grid if t in ("Sheet1", "Data") else []}
            for t in ("Sheet1", "Data", "R1C1", "RC", "A")
        ],
    }
    settings = build_corpus(tmp_path, [record], name="corpus.jsonl")
    with client_for(settings, reload=True) as client:
        h = {"Authorization": f"Bearer {settings.admin_token}"}
        (book,) = client.get(
            "/drive/v3/files", headers=h, params={"q": "name = 'backlot #174 R1C1 probe'"}
        ).json()["files"]
        sheets = client.get(f"/sheets/v4/spreadsheets/{book['id']}", headers=h).json()["sheets"]
        ids = {s["properties"]["title"]: s["properties"]["sheetId"] for s in sheets}
        tokens = {f"ID_{title.upper()}": ids[title] for title in ("Sheet1", "Data", "R1C1")}

        def detokenize(value):
            """A row's ``shown`` with the probe's sheet ids in place of their tokens: a whole
            token is an echoed `sheetId`, one inside a message is the id the message names."""
            if isinstance(value, str):
                if value in tokens:
                    return tokens[value]
                for token, sheet_id in tokens.items():
                    value = value.replace(token, str(sheet_id))
                return value
            if isinstance(value, (list, tuple)):
                return type(value)(detokenize(v) for v in value)
            if isinstance(value, dict):
                return {k: detokenize(v) for k, v in value.items()}
            return value

        drifted = []
        for route, target, body, status, shown in MEASURED_BY_FILTER:
            for token, sheet_id in tokens.items():
                body = body.replace(token.encode(), str(sheet_id).encode())
            spreadsheet = "nosuchspreadsheet" if target == "nosuch" else book["id"]
            path = f"/sheets/v4/spreadsheets/{spreadsheet}"
            path += "/values:batchGetByDataFilter" if route == "values" else ":getByDataFilter"
            xgafv = isinstance(shown, tuple)
            r = client.post(
                path,
                headers={} if target == "anon" else h,
                params={"$.xgafv": "1"} if xgafv else {},
                content=body,
            )
            got = _by_filter_shown(route, r, xgafv)
            if (r.status_code, got) != (status, detokenize(shown)):
                drifted.append(
                    f"{body!r}: real {status} {shown!r}, Backlot {r.status_code} {got!r}"
                )
                continue
            message = got[0] if xgafv else got
            if status == 400 and re.match(
                r"Invalid value[ (]|Invalid JSON payload received\. Unknown", message
            ):
                # a violation at the root of the body carries no `field` at all, not a null one
                want = [
                    {"field": m.group(1), "description": x}
                    if (m := re.search(r" at '([^']*)'", x))
                    else {"description": x}
                    for x in message.split("\n")
                ]
                fields = [v for d in r.json()["error"]["details"] for v in d["fieldViolations"]]
                if fields != want:
                    drifted.append(f"{body!r}: field violations {fields!r}")
        assert drifted == [], "\n".join(drifted)


_NOT_JSON = "Invalid JSON payload received. Unexpected token."


@pytest.mark.parametrize("route", ["/values:batchGetByDataFilter", ":getByDataFilter"])
@pytest.mark.parametrize(
    "body, message",
    [
        (b"abc", _NOT_JSON),
        (b'{"dataFilters": "abc"', _NOT_JSON),
        (b'{"a": NaN}', _NOT_JSON),
        (b'{"a": Infinity}', _NOT_JSON),
        (b'{"a": -Infinity}', _NOT_JSON),
        (b'{"a": 1e400}', _NOT_JSON),
        (b'{"a": -1e400}', _NOT_JSON),
        # the largest double, which is read
        (
            b'{"a": 1.7976931348623157e308}',
            'Invalid JSON payload received. Unknown name "a": Cannot find field.',
        ),
    ],
)
def test_a_body_that_does_not_parse_is_refused_before_the_lookup(
    gc, gh, book, route, body, message
):
    """The refusal `protojson.read` gives a body that does not parse, with a `parseError` entry at
    `$.xgafv=1` (`gerr.invalid_json`), the same for a spreadsheet that does not exist. These are not
    rows of `MEASURED_BY_FILTER`, since the sentence is not the one real gives."""
    for spreadsheet in (book, "nosuchspreadsheet"):
        r = gc.post(
            f"/sheets/v4/spreadsheets/{spreadsheet}{route}",
            headers=gh,
            params={"$.xgafv": "1"},
            content=body,
        )
        err = _gerr(r)
        assert (r.status_code, err["message"]) == (400, message)
        assert err["errors"][0]["reason"] == ("parseError" if message == _NOT_JSON else "invalid")


def test_get_by_data_filter_scopes_the_sheets_array_like_ranges_does(gc, gh, book):
    r = gc.post(
        f"/sheets/v4/spreadsheets/{book}:getByDataFilter",
        headers=gh,
        json={"dataFilters": [{"a1Range": "Summary!A1:B2"}], "includeGridData": True},
    )
    assert r.status_code == 200
    sheets = r.json()["sheets"]
    assert [s["properties"]["title"] for s in sheets] == ["Summary"]
    assert len(sheets[0]["data"]) == 1


def test_get_by_data_filter_takes_no_filters_to_mean_every_sheet(gc, gh, book):
    """Where its values-level sibling refuses an empty filter list, this one reads it as "all" —
    measured, and the two really do differ."""
    r = gc.post(f"/sheets/v4/spreadsheets/{book}:getByDataFilter", headers=gh, json={})
    assert r.status_code == 200
    assert len(r.json()["sheets"]) == 7


_NO_COMMENTS = "Field mask may not contain comment-specific fields if comments are not requested"
_NO_PERMISSION = "You do not have permission to view comments on this spreadsheet."
_INCLUDED, _OMITTED = "COMMENTS_VIEW_MODE_INCLUDED", "COMMENTS_VIEW_MODE_OMITTED"


@pytest.fixture(scope="module")
def dana(grid_settings):
    """The owner of every gridded record, where the admin token owns nothing and so reads them."""
    data = yaml.safe_load(grid_settings.tokens_path.read_text(encoding="utf-8"))
    return {"Authorization": f"Bearer {tok(data, 'dana@acme.com')}"}


@pytest.mark.parametrize(
    "route, mode, fields, owner, status, want",
    [
        ("get", None, None, True, 200, None),
        ("get", None, "commentsViewMode", False, 200, _OMITTED),
        ("get", "COMMENTS_VIEW_MODE_UNSPECIFIED", "commentsViewMode", True, 200, _OMITTED),
        ("get", "COMMENTS_VIEW_MODE_DEFAULT_FOR_CURRENT_ACCESS", None, True, 200, None),
        (
            "get",
            "COMMENTS_VIEW_MODE_DEFAULT_FOR_CURRENT_ACCESS",
            "commentsViewMode",
            True,
            200,
            _INCLUDED,
        ),
        (
            "get",
            "COMMENTS_VIEW_MODE_DEFAULT_FOR_CURRENT_ACCESS",
            "commentsViewMode",
            False,
            200,
            _OMITTED,
        ),
        ("get", "COMMENTS_VIEW_MODE_INCLUDED", "commentsViewMode", True, 200, _INCLUDED),
        ("get", "COMMENTS_VIEW_MODE_INCLUDED", "commentsViewMode", False, 403, _NO_PERMISSION),
        ("get", "+1", "commentsViewMode", True, 200, _INCLUDED),
        ("get", "03", "commentsViewMode", True, 200, _INCLUDED),
        ("get", "comments_view_mode_included", "commentsViewMode", True, 200, _INCLUDED),
        ("get", "COMMENTS_VIEW_MODE_OMITTED", "comments", True, 400, _NO_COMMENTS),
        ("get", None, "comments.headPost", True, 400, _NO_COMMENTS),
        ("get", None, "*.commentId", True, 400, _NO_COMMENTS),
        ("get", None, "*.*", True, 200, _OMITTED),
        ("get", "COMMENTS_VIEW_MODE_INCLUDED", "comments", True, 200, None),
        ("get", "COMMENTS_VIEW_MODE_INCLUDED", "comments", False, 403, _NO_PERMISSION),
        (
            "get",
            "COMMENTS_VIEW_MODE_DEFAULT_FOR_CURRENT_ACCESS",
            "comments.commentId",
            False,
            200,
            None,
        ),
        ("get", "4", None, True, 400, None),
        ("get", "", None, True, 400, None),
        ("getByDataFilter", None, "comments", True, 400, _NO_COMMENTS),
        ("getByDataFilter", 3, "comments", True, 200, None),
        ("getByDataFilter", 3, "comments", False, 403, _NO_PERMISSION),
        ("getByDataFilter", "1", "commentsViewMode", True, 200, _INCLUDED),
        ("getByDataFilter", "1", "commentsViewMode", False, 200, _OMITTED),
        # a number the enum does not declare is taken, and answered as no comments
        ("getByDataFilter", 4, "commentsViewMode", False, 200, _OMITTED),
        ("getByDataFilter", 4, "commentsViewMode", True, 200, _OMITTED),
        ("getByDataFilter", 4, "comments", False, 200, None),
        ("getByDataFilter", -1, "comments", True, 200, None),
    ],
)
def test_comments_view_mode_is_read_as_real_reads_it(
    gc, gh, dana, book, route, mode, fields, owner, status, want
):
    """`commentsViewMode` (`_sheets_comments_view`) in `spreadsheets.get`'s query and in
    `:getByDataFilter`'s body, each row as real answered it to the spreadsheet's owner or a reader.
    With no mask it shows only beside the grid: a mode alone answers none."""
    headers = dana if owner else gh
    params = {} if fields is None else {"fields": fields}
    if route == "get":
        params |= {} if mode is None else {"commentsViewMode": mode}
        r = gc.get(f"/sheets/v4/spreadsheets/{book}", headers=headers, params=params)
    else:
        body = {} if mode is None else {"commentsViewMode": mode}
        r = gc.post(
            f"/sheets/v4/spreadsheets/{book}:getByDataFilter",
            headers=headers,
            params=params,
            json=body,
        )
    assert r.status_code == status, r.text
    if status != 200:
        message = want or (
            "Invalid value at 'comments_view_mode' "
            f'(type.googleapis.com/google.apps.sheets.v4.CommentsViewMode), "{mode}"'
        )
        assert r.json()["error"]["message"] == message
    else:
        assert r.json().get("commentsViewMode") == want
        if fields is not None and "commentsViewMode" not in fields and "*" not in fields:
            assert r.json() == {}


_INCLUDE = {"commentsViewMode": "COMMENTS_VIEW_MODE_INCLUDED"}


@pytest.mark.parametrize(
    "method, url, params, body, status, message",
    [
        # a mask naming nothing: behind the typed values, ahead of the lookup, a range and `alt`
        (
            "GET",
            "{book}",
            {"fields": "bogus", "includeGridData": "x"},
            None,
            400,
            "include_grid_data",
        ),
        ("GET", "{missing}", {"fields": "bogus"}, None, 400, "Request contains"),
        ("GET", "{book}", {"fields": "bogus", "ranges": "Nope!A1"}, None, 400, "Request contains"),
        ("GET", "{book}", {"fields": "bogus", "alt": "media"}, None, 400, "Request contains"),
        ("GET", "{missing}/values/A1", {"fields": "bogus"}, None, 400, "Request contains"),
        (
            "GET",
            "{missing}/values:batchGet",
            {"ranges": "A1", "fields": "bogus"},
            None,
            400,
            "Request contains",
        ),
        ("POST", "{missing}:getByDataFilter", {"fields": "bogus"}, {}, 400, "Request contains"),
        (
            "POST",
            "{missing}/values:batchGetByDataFilter",
            {"fields": "bogus"},
            {"dataFilters": [{"a1Range": "A1"}]},
            400,
            "Request contains",
        ),
        (
            "POST",
            "{book}/values:batchGetByDataFilter",
            {"fields": "bogus"},
            {},
            400,
            "Request contains",
        ),
        (
            "POST",
            "{book}:getByDataFilter",
            {"fields": "bogus"},
            {"includeGridData": "x"},
            400,
            "include_grid_data",
        ),
        # a comment field: behind the lookup, ahead of `ranges` and `alt=media`, and behind a
        # filter on `:getByDataFilter`
        ("GET", "{missing}", {"fields": "comments"}, None, 404, "Requested entity"),
        ("GET", "{book}", {"fields": "comments", "ranges": "Nope!A1"}, None, 400, "Field mask"),
        ("GET", "{book}", {"fields": "comments", "alt": "media"}, None, 400, "Field mask"),
        (
            "POST",
            "{book}:getByDataFilter",
            {"fields": "comments"},
            {"dataFilters": [{"a1Range": "Nope!A1"}]},
            400,
            "Unable to parse range",
        ),
        # an `alt` naming no format: ahead of the typed values and the body
        ("GET", "{book}", {"alt": "zzz", "includeGridData": "x"}, None, 400, 'Invalid value "zzz"'),
        (
            "POST",
            "{book}:getByDataFilter",
            {"alt": "zzz"},
            {"includeGridData": "x"},
            400,
            'Invalid value "zzz"',
        ),
        ("GET", "{missing}", {"alt": "media"}, None, 404, "Requested entity"),
        # a reader asking for comments: behind the mask, ahead of `ranges` and `alt=media`, and
        # behind a filter
        ("GET", "{book}", {**_INCLUDE, "fields": "bogus"}, None, 400, "Request contains"),
        ("GET", "{book}", {**_INCLUDE, "ranges": "Nope!A1"}, None, 403, "You do not have"),
        ("GET", "{book}", {**_INCLUDE, "alt": "media"}, None, 403, "You do not have"),
        (
            "POST",
            "{book}:getByDataFilter",
            {},
            {**_INCLUDE, "dataFilters": [{"a1Range": "Nope!A1"}]},
            400,
            "Unable to parse range",
        ),
    ],
)
def test_a_sheets_read_answers_its_refusals_in_reals_order(
    gc, gh, book, method, url, params, body, status, message
):
    """Each pair of refusals as real ordered them (`_sheets_alt`, `_sheets_mask`,
    `_sheets_comments_view`), `message` the start of the one it gave, or the proto field a typed
    refusal names."""
    url = url.format(book=book, missing="1nonexistentAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA")
    r = gc.request(method, f"/sheets/v4/spreadsheets/{url}", headers=gh, params=params, json=body)
    assert r.status_code == status, r.text
    err = r.json()["error"]
    assert err["message"].startswith(message) or message in err["message"]


@pytest.mark.parametrize(
    "fields, want",
    [
        ("valueRanges.dataFilters.developerMetadataLookup", [{"dataFilters": [{}]}] * 2),
        (
            "valueRanges.dataFilters.gridRange.sheetId",
            [{"dataFilters": [{"gridRange": {}}]}, {"dataFilters": [{}]}],
        ),
        (
            "valueRanges.dataFilters.gridRange.endRowIndex",
            [{"dataFilters": [{"gridRange": {"endRowIndex": 1}}]}, {"dataFilters": [{}]}],
        ),
        (
            "valueRanges.dataFilters.developerMetadataLookup.metadataLocation.dimensionRange.sheetId",
            [{"dataFilters": [{}]}] * 2,
        ),
    ],
)
def test_a_values_level_mask_may_name_what_a_filter_declares(gc, gh, book, fields, want):
    """A `DataFilter`'s declared fields answer 200, the lookup no filter here holds absent, and
    `gridRange.sheetId` absent at its default of 0, as real answered."""
    body = {
        "dataFilters": [
            {"gridRange": {"sheetId": 0, "startRowIndex": 0, "endRowIndex": 1}},
            {"a1Range": "Summary!B1:B2"},
        ]
    }
    r = gc.post(
        f"/sheets/v4/spreadsheets/{book}/values:batchGetByDataFilter",
        headers=gh,
        params={"fields": fields},
        json=body,
    )
    assert r.status_code == 200, r.text
    assert r.json() == {"valueRanges": want}


@pytest.mark.parametrize(
    "key, field, enum",
    [
        ("majorDimension", "major_dimension", "Dimension"),
        ("valueRenderOption", "value_render_option", "ValueRenderOption"),
        ("dateTimeRenderOption", "date_time_render_option", "DateTimeRenderOption"),
    ],
)
@pytest.mark.parametrize("value", ["NOPE", ""])
def test_a_read_enum_carried_in_the_body_follows_the_query_strings_rule(
    gc, gh, book, key, field, enum, value
):
    """The by-data-filter read takes its enums in the request body, and for a string value the rule
    must not fork: ASCII case ignored, an empty value refused rather than defaulted, and
    `dateTimeRenderOption` validated even though a corpus states no date cell for it to render."""
    r = _by_filter(gc, gh, book, {"dataFilters": [{"a1Range": "Summary!A1"}], key: value})
    assert r.status_code == 400, r.text
    assert r.json()["error"]["message"] == (
        f"Invalid value at '{field}' (type.googleapis.com/google.apps.sheets.v4.{enum}), \"{value}\""
    )


@pytest.mark.parametrize(
    "key, value", [("majorDimension", "columns"), ("valueRenderOption", "unformatted_value")]
)
def test_a_read_enum_in_the_body_is_matched_case_insensitively(gc, gh, book, key, value):
    r = _by_filter(gc, gh, book, {"dataFilters": [{"a1Range": "Summary!A1:B2"}], key: value})
    assert r.status_code == 200, r.text


# --- a cell's effectiveFormat -------------------------------------------------------------------

_CELL_FORMAT = {
    "backgroundColor": {"red": 1, "green": 1, "blue": 1},
    "padding": {"top": 2, "right": 3, "bottom": 2, "left": 3},
    "verticalAlignment": "BOTTOM",
    "wrapStrategy": "OVERFLOW_CELL",
    "textFormat": {
        "foregroundColor": {},
        "fontFamily": "Arial",
        "fontSize": 10,
        "bold": False,
        "italic": False,
        "strikethrough": False,
        "underline": False,
        "foregroundColorStyle": {"rgbColor": {}},
    },
    "hyperlinkDisplayType": "PLAIN_TEXT",
    "backgroundColorStyle": {"rgbColor": {"red": 1, "green": 1, "blue": 1}},
}


def _cells(gc, gh, book, rng):
    r = gc.get(
        f"/sheets/v4/spreadsheets/{book}",
        headers=gh,
        params={"includeGridData": "true", "ranges": rng},
    )
    assert r.status_code == 200, r.text
    return r.json()["sheets"][0]["data"][0]["rowData"]


@pytest.mark.parametrize(
    "rng, align",
    [
        ("Summary!A1:A1", "LEFT"),  # a string
        ("Summary!B2:B2", "RIGHT"),  # a number
        ("Summary!C2:C2", "CENTER"),  # a boolean
    ],
)
def test_a_cell_carries_the_format_its_type_gives_it(gc, gh, book, rng, align):
    """Measured: `effectiveFormat` is the spreadsheet's default plus a `horizontalAlignment` the
    cell's TYPE decides — a string left, a number right, a boolean centred. Nothing else varies
    across the types a corpus can state, so the rest is a constant."""
    cell = _cells(gc, gh, book, rng)[0]["values"][0]
    assert cell["effectiveFormat"] == {**_CELL_FORMAT, "horizontalAlignment": align}


def test_an_empty_cell_carries_no_format_either(gc, gh, book):
    """A corpus states a cell as `null` meaning nothing is there, which is real's never-written
    cell — and measured, that one comes back as `{}` with no format. (Real distinguishes a cell
    someone wrote an empty string into, which keeps a format and loses its value; a corpus has no
    way to say that, so there is nothing to reproduce.)"""
    assert _cells(gc, gh, book, "Ragged!A1:C1")[0]["values"][1] == {}


def test_the_cell_format_is_emitted_in_the_order_real_sends_it(gc, gh, book):
    """A client diffing two backends byte for byte sees key order, and proto3 fixes it."""
    r = gc.get(
        f"/sheets/v4/spreadsheets/{book}",
        headers=gh,
        params={"includeGridData": "true", "ranges": "Summary!A1:A1"},
    )
    fmt = r.json()["sheets"][0]["data"][0]["rowData"][0]["values"][0]["effectiveFormat"]
    assert list(fmt) == [
        "backgroundColor",
        "padding",
        "horizontalAlignment",
        "verticalAlignment",
        "wrapStrategy",
        "textFormat",
        "hyperlinkDisplayType",
        "backgroundColorStyle",
    ]
    assert list(fmt["textFormat"]) == [
        "foregroundColor",
        "fontFamily",
        "fontSize",
        "bold",
        "italic",
        "strikethrough",
        "underline",
        "foregroundColorStyle",
    ]


def test_a_fields_mask_may_now_name_the_cell_format(gc, gh, book):
    """The caveat this closes: a mask reaching into `effectiveFormat` was refused while the real
    API answered it."""
    r = gc.get(
        f"/sheets/v4/spreadsheets/{book}",
        headers=gh,
        params={
            "includeGridData": "true",
            "ranges": "Summary!A1:A1",
            "fields": "sheets.data.rowData.values.effectiveFormat.horizontalAlignment",
        },
    )
    assert r.status_code == 200, r.text
    assert r.json()["sheets"][0]["data"][0]["rowData"][0]["values"][0] == {
        "effectiveFormat": {"horizontalAlignment": "LEFT"}
    }


def test_the_spreadsheet_carries_the_default_format_and_theme_a_fresh_one_has(gc, gh, book):
    """`properties.defaultFormat` and `properties.spreadsheetTheme` are per-workbook SETTINGS, not
    derivations — measured across six real workbooks they came in three variants, differing where
    someone had applied a theme or imported from .xlsx (Malgun Gothic, MIDDLE alignment).

    A corpus states none of that, so what is served is the variant a freshly created spreadsheet
    has, on the same footing as `locale`, `autoRecalc`, `timeZone` and the 1000x26 grid: the value
    a document nobody customised carries. Serving nothing instead would withhold a field real
    always sends."""
    props = gc.get(f"/sheets/v4/spreadsheets/{book}", headers=gh).json()["properties"]
    assert props["defaultFormat"] == {
        "backgroundColor": {"red": 1, "green": 1, "blue": 1},
        "padding": {"top": 2, "right": 3, "bottom": 2, "left": 3},
        "verticalAlignment": "BOTTOM",
        "wrapStrategy": "OVERFLOW_CELL",
        "textFormat": {
            "foregroundColor": {},
            # the spreadsheet default is the CSS stack; a cell's own textFormat says "Arial"
            "fontFamily": "arial,sans,sans-serif",
            "fontSize": 10,
            "bold": False,
            "italic": False,
            "strikethrough": False,
            "underline": False,
            "foregroundColorStyle": {"rgbColor": {}},
        },
        "backgroundColorStyle": {"rgbColor": {"red": 1, "green": 1, "blue": 1}},
    }
    theme = props["spreadsheetTheme"]
    assert theme["primaryFontFamily"] == "Arial"
    assert [c["colorType"] for c in theme["themeColors"]] == [
        "TEXT",
        "BACKGROUND",
        "ACCENT1",
        "ACCENT2",
        "ACCENT3",
        "ACCENT4",
        "ACCENT5",
        "ACCENT6",
        "LINK",
    ]
    # TEXT is the one whose rgbColor is empty — proto3 drops a zero, so black is `{}`
    assert theme["themeColors"][0]["color"] == {"rgbColor": {}}
    assert theme["themeColors"][2]["color"]["rgbColor"] == {
        "red": 0.25882354,
        "green": 0.52156866,
        "blue": 0.95686275,
    }


def test_the_properties_are_emitted_in_the_order_real_sends_them(gc, gh, book):
    url = f"/sheets/v4/spreadsheets/{book}"
    body = gc.get(url, headers=gh).json()
    assert list(body) == _BOOK_KEYS
    grid = gc.get(url, headers=gh, params={"includeGridData": "true", "ranges": "Summary!A1"})
    assert list(grid.json()) == [*_BOOK_KEYS, "commentsViewMode"]
    props = body["properties"]
    assert list(props) == [
        "title",
        "locale",
        "autoRecalc",
        "timeZone",
        "defaultFormat",
        "spreadsheetTheme",
    ]
    assert list(props["defaultFormat"]) == [
        "backgroundColor",
        "padding",
        "verticalAlignment",
        "wrapStrategy",
        "textFormat",
        "backgroundColorStyle",
    ]


def test_a_fields_mask_may_now_name_the_spreadsheet_format(gc, gh, book):
    r = gc.get(
        f"/sheets/v4/spreadsheets/{book}",
        headers=gh,
        params={
            "fields": "properties(defaultFormat.textFormat.fontSize,spreadsheetTheme.primaryFontFamily)"
        },
    )
    assert r.status_code == 200, r.text
    assert r.json() == {
        "properties": {
            "defaultFormat": {"textFormat": {"fontSize": 10}},
            "spreadsheetTheme": {"primaryFontFamily": "Arial"},
        }
    }


@pytest.mark.parametrize(
    "mask",
    [
        "properties.defaultFormat.textFormat.foregroundColorStyle.rgbColor.red",
        "properties.defaultFormat.backgroundColorStyle.rgbColor.blue",
        "properties.spreadsheetTheme.themeColors.color.rgbColor.green",
        "sheets.data.rowData.values.effectiveFormat.textFormat.foregroundColorStyle.rgbColor.red",
        "sheets.data.rowData.values.effectiveFormat.backgroundColor.alpha",
    ],
)
def test_a_mask_may_name_a_colour_component_wherever_a_colour_appears(gc, gh, book, mask):
    """Every Color in the response takes the same mask depth. Left as a leaf in one tree and spelled
    out in another, `...rgbColor.red` was refused under a cell and answered under the spreadsheet —
    where the real API answers both."""
    r = gc.get(
        f"/sheets/v4/spreadsheets/{book}",
        headers=gh,
        params={"includeGridData": "true", "ranges": "Summary!A1:A1", "fields": mask},
    )
    assert r.status_code == 200, r.text


@pytest.mark.parametrize(
    "mask, include, wants_grid",
    [
        ("sheets.data.rowData.values.formattedValue", None, True),
        ("sheets", "false", True),
        ("sheets.*", "false", True),
        ("*.data", None, True),
        ("*.*", None, True),
        ("sheets.properties.title", "true", False),
        ("spreadsheetId", "true", False),
        # no mask at all, so `includeGridData` decides
        ("*", None, False),
        ("*", "true", True),
        ("a()", "true", True),
    ],
)
def test_a_field_mask_decides_the_grid_when_one_is_set(gc, gh, book, mask, include, wants_grid):
    """The vendor's own wording for `includeGridData`: "This parameter is ignored if a field mask
    was set in the request." Measured, that is narrower than it reads — the mask has to reach
    `sheets.data` (`_gmask_wants_grid`)."""
    params = {"fields": mask, "ranges": "Summary!A1:A1"}
    if include is not None:
        params["includeGridData"] = include
    r = gc.get(f"/sheets/v4/spreadsheets/{book}", headers=gh, params=params)
    assert r.status_code == 200, r.text
    sheets = r.json().get("sheets") or [{}]
    assert ("data" in sheets[0]) is wants_grid


@pytest.mark.parametrize(
    "value, grid",
    [("false", False), ("no", False), ("0", False), (False, False), ("true", True), (1, True)],
)
def test_include_grid_data_in_the_body_is_read_as_a_proto_bool(gc, gh, book, value, grid):
    """`includeGridData` in the body, read by `protojson.to_bool`: a string by its words, a number
    when it is 0 or 1."""
    r = gc.post(
        f"/sheets/v4/spreadsheets/{book}:getByDataFilter",
        headers=gh,
        json={"dataFilters": [{"a1Range": "Summary!A1"}], "includeGridData": value},
    )
    assert r.status_code == 200, r.text
    assert ("data" in r.json()["sheets"][0]) is grid


@pytest.mark.parametrize(
    "title,content",
    [("ASCII", "hello there"), ("ASCII", "안녕하세요"), ("회의 일정", "😀 café")],
)
def test_gmail_size_estimate_matches_raw_bytes_in_every_format(tmp_path, title, content):
    """The `sizeEstimate` rule in `_byte_len`, under every `format` and in the thread. `raw`
    carries a non-ASCII body transfer-encoded and a non-ASCII subject as an encoded-word, longer
    than the subject's text, so the third row tells a size counted from the served `raw` apart
    from one counted before its headers are encoded.
    """
    s = tiny_corpus(
        tmp_path,
        [
            {
                "source_type": "gmail",
                "doc_id": "size",
                "mailbox": "owner",
                "title": title,
                "author_email": "owner@example.com",
                "created": "2026-10-01T00:00:00Z",
                "content": content,
            }
        ],
    )
    with client_for(s) as c:
        h = {
            "Authorization": "Bearer "
            + yaml.safe_load(s.tokens_path.read_text(encoding="utf-8"))["admin_token"]
        }
        mid = c.get("/gmail/v1/users/me/messages", headers=h).json()["messages"][0]["id"]
        path = "/gmail/v1/users/me/messages/" + mid
        raw = c.get(path + "?format=raw", headers=h).json()
        encoded = raw["raw"]
        size = len(base64.urlsafe_b64decode(encoded + "=" * (-len(encoded) % 4)))
        assert raw["sizeEstimate"] == size
        for fmt in ["minimal", "metadata", "full"]:
            message = c.get(path + "?format=" + fmt, headers=h).json()
            assert message["sizeEstimate"] == size
            tid = message["threadId"]
            thread = c.get(f"/gmail/v1/users/me/threads/{tid}?format={fmt}", headers=h)
            assert thread.status_code == 200
            assert thread.json()["messages"][0]["sizeEstimate"] == size


@pytest.mark.parametrize("surface", ["messages", "threads"])
@pytest.mark.parametrize(
    "caller, query, expected",
    [
        ("ava", "cc:lena", ["plan"]),
        ("ava", "cc:LENA", ["plan"]),
        ("ava", "cc:LENA@ACME.COM", ["plan"]),
        ("ava", 'cc:"lena@acme.com" rollout', ["plan"]),
        ("ava", "bcc:ava", ["lunch"]),
        ("ava", "cc:acme.com", ["plan"]),
        ("ava", "cc:lena bcc:ava", []),
        ("ava", "cc:nobody", []),
        ("ava", "bcc:me", ["lunch"]),
        ("ava", "cc:me", ["plan"]),
        ("admin", "cc:me", []),
        # a word, never part of one, and an address only whole
        ("ava", "cc:len", []),
        ("ava", "cc:ena", []),
        ("ava", "cc:acme", ["plan"]),
        ("ava", "cc:com", ["plan"]),
        ("ava", "cc:com.acme", []),
        ("ava", "cc:@acme.com", ["plan"]),
        ("ava", "cc:lena@", ["plan"]),
        ("ava", "cc:lena@acme", []),
        ("ava", "cc:park", ["plan"]),
        ("ava", "cc:par", []),
        ("ava", 'cc:"lena park"', ["plan"]),
        ("ava", "bcc:\uc5d0\ubc14", ["lunch"]),
        ("ava", "cc:ops_team", ["standup"]),
        ("ava", "cc:ops", []),
        ("ava", "cc:_", []),
        ("ava", "cc:&", []),
        # no word at all: the operator is ignored
        ("ava", "cc:.", ["plan", "lunch", "decoy", "standup"]),
        ("ava", "cc:@", ["plan", "lunch", "decoy", "standup"]),
        ("ava", 'bcc:""', ["plan", "lunch", "decoy", "standup"]),
    ],
)
def test_gmail_cc_bcc_searches_address_headers(tmp_path, surface, caller, query, expected):
    """The rule `_gmail_address_match` records, on both listings: a message whose body or subject
    names the address is not a match. `me` is the caller's address, and for the admin token, which
    has no mailbox of its own, the address `_service_email` gives."""
    records = [
        {
            "doc_id": "plan",
            "title": "rollout plan",
            "cc": "Lena Park <lena@acme.com>, ava@acme.com",
        },
        {"doc_id": "lunch", "title": "lunch", "bcc": "\uc5d0\ubc14 <ava@acme.com>"},
        {"doc_id": "decoy", "title": "cc lena bcc ava", "content": "lena ava"},
        {"doc_id": "standup", "title": "standup", "cc": "ops_team@corp.example, me@corp.example"},
    ]
    settings = tiny_corpus(
        tmp_path,
        [
            {
                "source_type": "gmail",
                "mailbox": "ava",
                "author_email": "bob@acme.com",
                "to": "ava@acme.com",
                "content": "body",
                "visibility": "public",
                "created": "2026-03-01T09:00:00Z",
                "readers": ["ava@acme.com"],
                **r,
            }
            for r in records
        ],
    )
    tokens = yaml.safe_load(settings.tokens_path.read_text(encoding="utf-8"))
    token = tokens["admin_token"] if caller == "admin" else tok(tokens, "ava@acme.com")
    with client_for(settings, reload=True) as c:
        response = c.get(
            f"/gmail/v1/users/me/{surface}",
            headers={"Authorization": f"Bearer {token}"},
            params={"q": query},
        )
    assert response.status_code == 200
    assert {r["id"] for r in response.json().get(surface, [])} == {
        served_id("gmail", doc_id) for doc_id in expected
    }


@pytest.mark.parametrize("surface", ["messages", "threads"])
@pytest.mark.parametrize(
    "caller, query, expected",
    [
        ("ava", "to:ava@acme.com", ["plan", "lunch"]),
        ("ava", "to:me", ["plan", "lunch"]),
        ("ava", "to:av", []),
        ("ava", "from:bo", []),
        ("ava", "from:me", ["reply"]),
        ("ava", 'from:"me"', ["reply"]),
        ("ava", 'to:"me"', ["plan", "lunch"]),
        ("ava", "from:", ["plan", "lunch", "reply"]),
        ("ava", "to:dev@acme.com", ["reply"]),
        ("ava", "to:dev", ["reply"]),
        ("ava", "to:lena@acme.com", ["lunch"]),
        ("ava", "to:lena", ["lunch"]),
        ("ava", "to:bob@acme.com", ["reply"]),
        ("ava", "to:bob", ["reply"]),
        ("ava", "from:bob@acme.com", ["plan"]),
        ("ava", "from:bob", ["plan"]),
        ("ava", "from:acme.com", ["plan", "lunch", "reply"]),
        ("ava", "from:acm", []),
        ("ava", "to:acme.com", ["plan", "lunch", "reply"]),
        ("ava", "to:acm", []),
        ("ava", "to:le", []),
        ("ava", "to:", ["plan", "lunch", "reply"]),
        ("ava", "bcc:", ["plan", "lunch", "reply"]),
        ("ava", "cc:", ["plan", "lunch", "reply"]),
        ("ava", 'from:""', ["plan", "lunch", "reply"]),
        ("ava", 'to:""', ["plan", "lunch", "reply"]),
        ("ava", "to: from: bcc:", []),
        ("ava", "from: bob", ["plan"]),
        ("ava", 'from:"" bob', ["plan"]),
        ("ava", 'to:" " lena', ["lunch"]),
        ("ava", 'cc:""dev', ["reply"]),
        ("ava", "From: bob", ["plan"]),
        ("ava", "to:\tlena", ["lunch"]),
        ("ava", "cc: dev", ["reply"]),
        ("ava", "bcc: ava", ["lunch"]),
        ("ava", "to:bob to:dev", ["reply"]),
        ("ava", "from:bob from:mia", []),
        ("ava", "from:bob from:", ["plan"]),
        ("ava", 'to:ava to:""', ["plan", "lunch"]),
        ("admin", "from:me", []),
        ("admin", "to:me", []),
        ("admin", "from:ava@acme.com", ["reply"]),
        ("admin", "to:ava@acme.com", ["plan", "lunch"]),
        ("admin", "from:", ["plan", "lunch", "reply"]),
        # a `-` in front of the operator or its value negates it
        ("ava", "-from:bob", ["lunch", "reply"]),
        ("ava", "from:-bob", ["lunch", "reply"]),
        ("ava", "-from: bob", ["lunch", "reply"]),
        ("ava", 'from:"-bob"', ["plan"]),
        ("ava", "-to:lena", ["plan", "reply"]),
        ("ava", "-cc:dev", ["plan", "lunch"]),
        ("ava", "bcc:-ava", ["plan", "reply"]),
        ("ava", "-from:me", ["plan", "lunch"]),
        ("ava", '-from:"me"', ["plan", "lunch"]),
        ("ava", "-from:bob from:mia", ["lunch"]),
        ("ava", '-from:""', ["plan", "lunch", "reply"]),
        ("ava", "from:-", ["plan", "lunch", "reply"]),
        ("admin", "-from:me", ["plan", "lunch", "reply"]),
    ],
)
def test_gmail_from_to_searches_address_headers(tmp_path, surface, caller, query, expected):
    """A `from:` search matches author email, and `to:` matches any recipient header (To, Cc, or
    Bcc) as whole words or address. `me` resolves to the caller's address, and for admin callers,
    the address `_service_email` gives. Empty address operators are ignored, and a `-` in front of
    an operator or its value negates it (`_GMAIL_NEGATED_ADDRESS`)."""
    records = [
        {
            "doc_id": "plan",
            "title": "Rollout plan",
            "content": "attached",
            "author_email": "bob@acme.com",
            "to": "ava@acme.com",
            "readers": ["ava@acme.com"],
            "created": "2026-03-01T09:00:00Z",
        },
        {
            "doc_id": "lunch",
            "title": "Lunch",
            "content": "Friday?",
            "author_email": "mia@acme.com",
            "to": "lena@acme.com",
            "bcc": "ava@acme.com",
            "readers": ["ava@acme.com"],
            "created": "2026-03-02T09:00:00Z",
        },
        {
            "doc_id": "reply",
            "title": "Re: Rollout plan",
            "content": "ok",
            "author_email": "ava@acme.com",
            "to": "bob@acme.com",
            "cc": "dev@acme.com",
            "readers": ["ava@acme.com"],
            "created": "2026-03-03T09:00:00Z",
        },
    ]
    settings = tiny_corpus(
        tmp_path,
        [
            {
                "source_type": "gmail",
                "mailbox": "ava",
                "visibility": "public",
                **r,
            }
            for r in records
        ],
    )
    tokens = yaml.safe_load(settings.tokens_path.read_text(encoding="utf-8"))
    token = tokens["admin_token"] if caller == "admin" else tok(tokens, f"{caller}@acme.com")
    with client_for(settings, reload=True) as c:
        response = c.get(
            f"/gmail/v1/users/me/{surface}",
            headers={"Authorization": f"Bearer {token}"},
            params={"q": query},
        )
    assert response.status_code == 200
    assert {r["id"] for r in response.json().get(surface, [])} == {
        served_id("gmail", doc_id) for doc_id in expected
    }


@pytest.mark.parametrize("surface", ["messages", "threads"])
@pytest.mark.parametrize(
    "query, expected",
    [
        ("subject:", ["plan", "lunch", "after"]),
        ("subject: ", ["plan", "lunch", "after"]),
        ("subject:\t", ["plan", "lunch", "after"]),
        ("SUBJECT:", ["plan", "lunch", "after"]),
        ("label:", ["plan", "lunch", "after"]),
        ('label:""', ["plan", "lunch", "after"]),
        ("in:", ["plan", "lunch", "after"]),
        ('in:""', ["plan", "lunch", "after"]),
        ("lunch label:", ["lunch"]),
        ('subject:"" lunch', ["lunch"]),
        ('subject:" " lunch', ["lunch"]),
        ('label:"" lunch', []),
        ("after:", ["after"]),
        ('label:"', ["plan", "lunch", "after"]),
        ('in:"', ["plan", "lunch", "after"]),
        ('subject:"', ["plan", "lunch", "after"]),
        ('lunch label:"', ["lunch"]),
        ('label:" lunch', []),
        ('label: " inbox', ["plan", "lunch", "after"]),
        ('label:" -inbox', ["plan", "lunch", "after"]),
        ('label:" inbox lunch', []),
        ('subject:" lunch', ["lunch"]),
        ('subject:" after', []),
        ('subject:" rollout plan', ["plan"]),
        ('subject:" plan rollout', []),
        ('subject:" label:inbox', []),
        ('subject:" after:2026/01/01', []),
        ('LABEL:" inbox', ["plan", "lunch", "after"]),
        ('subject:" from:me', ["plan", "lunch", "after"]),
        ('subject:" FROM:me', ["plan", "lunch", "after"]),
        ('subject:" from: me', ["plan", "lunch", "after"]),
        ('subject:" -from:me', ["plan", "lunch", "after"]),
        ('subject:" from:-me', ["plan", "lunch", "after"]),
        ('subject:"from:me', []),
        ('subject:" from:me+lunch', []),
        ('-subject:" from:me', []),
        ('-subject:" from:bob', ["plan", "lunch", "after"]),
        ('in:" subject:lunch', ["lunch"]),
    ],
)
def test_gmail_an_empty_or_lone_quoted_operator(tmp_path, surface, query, expected):
    """Each row is how the comments on `_GMAIL_DROPPED_WHEN_EMPTY`, `_GMAIL_SPACED` and
    `_GMAIL_LONE_QUOTE` read its query, over both listings; the corpus holds the word `after` so
    that a bare `after:` can be told from a dropped one, and `subject:" after` from a search
    term."""
    settings = tiny_corpus(
        tmp_path,
        [
            {
                "source_type": "gmail",
                "doc_id": doc_id,
                "mailbox": "ava",
                "title": title,
                "content": content,
                "author_email": "ava@acme.com",
                "readers": ["ava@acme.com"],
                "created": "2026-03-01T09:00:00Z",
            }
            for doc_id, title, content in [
                ("plan", "Rollout plan", "attached"),
                ("lunch", "Lunch", "Friday?"),
                ("after", "Later", "see after the meeting"),
            ]
        ],
    )
    tokens = yaml.safe_load(settings.tokens_path.read_text(encoding="utf-8"))
    with client_for(settings, reload=True) as c:
        r = c.get(
            f"/gmail/v1/users/me/{surface}",
            headers={"Authorization": f"Bearer {tok(tokens, 'ava@acme.com')}"},
            params={"q": query},
        )
    assert r.status_code == 200
    assert {item["id"] for item in r.json().get(surface, [])} == {
        served_id("gmail", doc_id) for doc_id in expected
    }


@pytest.mark.parametrize("surface", ["messages", "threads"])
@pytest.mark.parametrize(
    "query, expected",
    [
        ('from:" me', ["lunch"]),
        ('to:" me', ["plan"]),
        ('from:" probe', []),
        ('from:"me probe', []),
        ('to:" probe', []),
        ('from:" label:inbox', []),
        ('subject:" from:me"', ["lunch"]),
        ('subject:" from:"me"', ["lunch"]),
        ('label:" inbox "', ["plan"]),
        ('subject:" probe "', ["lunch"]),
        ('subject:" probe\u00a0"', ["lunch"]),
        ('subject:" from:" probe', []),
        ('label:" inbox subject:"probe"', ["plan"]),
        ('" label:inbox', ["plan"]),
        ('" from:me', ["lunch"]),
        ('newer_than:"', []),
        ('label:"" "', ["plan", "lunch"]),
        ('label:"""', ["plan", "lunch"]),
        ("probe-label:inbox", []),
        ('probe-subject:" from:me', []),
        ("probe-(from:me)", []),
        ('from:" from:me', ["lunch"]),
        ('to:" from:me', ["lunch"]),
        ('cc:" from:me', ["lunch"]),
        ('to:" -from:me', ["lunch"]),
        ('from:" to:me', ["plan"]),
        ('from:" -to:me', ["plan"]),
        ('-from:" to:me', ["lunch"]),
        ('from:" subject:probe', ["lunch"]),
        ('label:"" " inbox', ["plan"]),
        ('subject:"" " from:me', ["lunch"]),
        ('subject:"" " -from:me', ["lunch"]),
        ('in:"" " to:me', ["plan"]),
        ('subject:"" " label:inbox', []),
        ('label:" inbox -subject:"probe"', ["plan"]),
        ('-newer_than:"', ["plan", "lunch"]),
        ('NOT older_than:"', ["plan", "lunch"]),
        ('newer_than:""', []),
        ('older_than:""', []),
        ('newer_than:"" "', []),
        ('older_than:30d"', ["plan", "lunch"]),
        ('older_than:30d"" "', ["plan", "lunch"]),
        ('newer_than:30"d', []),
        ("2026-label:inbox", ["plan"]),
        ("2026-from:me", []),
        ("probe-(label:inbox)", ["plan"]),
        ("probe-(label:inbox", ["plan"]),
        ("probe-(label:inbox from:me)", []),
        ("probe-(-from:me)", []),
        ("probe-(label:inbox -subject:probe)", []),
        ("-probe-label:inbox", ["plan", "lunch"]),
        ("NOT probe-label:inbox", ["plan", "lunch"]),
        ("NOT NOT probe-label:inbox", []),
        ("-probe-(from:me)", ["plan", "lunch"]),
        ("-probe-label:inbox from:me", ["lunch"]),
        ('" -label:inbox', ["plan"]),
        ('" -from:me', ["lunch"]),
        ('label:inbox "', ["plan"]),
        ('"label:inbox', ["plan"]),
        ('" label:inbox -from:me', []),
        ('label:inbox -from:me "', ["plan"]),
        ('" label:', []),
        ('" from:', []),
        ("label:inbox me", []),
        ('from:"', ["plan", "lunch"]),
        ('probe from:"', ["plan", "lunch"]),
        ('from:"me', ["lunch"]),
        ('subject:"', ["plan", "lunch"]),
        ('-subject:"', ["plan", "lunch"]),
        ('probe-label:" inbox', []),
        ('" probe', ["plan", "lunch"]),
        ('probe "', ["plan", "lunch"]),
        ('"probe', ["plan", "lunch"]),
        ('"', []),
    ],
)
def test_gmail_a_stray_quote_beside_an_operator(tmp_path, surface, query, expected):
    """Each row is how the comments on `_GMAIL_LONE_QUOTE`, `_GMAIL_CLOSED_QUOTE`,
    `_GMAIL_HYPHENED` and `_GMAIL_BARE_LONE_QUOTE`, and those in `_parse_gmail_q`, read its query,
    over both listings. `plan` is from bob to the caller in the inbox and `lunch` from the caller to
    bob in sent; both hold the word `probe` and the number 2026."""
    settings = tiny_corpus(
        tmp_path,
        [
            {
                "source_type": "gmail",
                "doc_id": doc_id,
                "mailbox": "ava",
                "title": title,
                "content": content,
                "author_email": author,
                "to": to,
                "label_ids": [label],
                "readers": ["ava@acme.com"],
                "created": "2026-03-01T09:00:00Z",
            }
            for doc_id, title, content, author, to, label in [
                ("plan", "Rollout plan", "probe 2026", "bob@acme.com", "ava@acme.com", "INBOX"),
                ("lunch", "Lunch probe", "Friday? 2026", "ava@acme.com", "bob@acme.com", "SENT"),
            ]
        ],
    )
    tokens = yaml.safe_load(settings.tokens_path.read_text(encoding="utf-8"))
    with client_for(settings, reload=True) as c:
        r = c.get(
            f"/gmail/v1/users/me/{surface}",
            headers={"Authorization": f"Bearer {tok(tokens, 'ava@acme.com')}"},
            params={"q": query},
        )
    assert r.status_code == 200
    assert {item["id"] for item in r.json().get(surface, [])} == {
        served_id("gmail", doc_id) for doc_id in expected
    }


@pytest.mark.parametrize("surface", ["messages", "threads"])
@pytest.mark.parametrize(
    "query, expected", [('subject:"from: foo"', ["literal"]), ('subject:""', ["literal", "other"])]
)
def test_gmail_empty_address_tokens_preserve_quoted_subjects(tmp_path, surface, query, expected):
    """Ignoring an empty address operator must not edit a quoted value or change empty subjects."""
    settings = tiny_corpus(
        tmp_path,
        [
            {
                "source_type": "gmail",
                "doc_id": doc_id,
                "mailbox": "ava",
                "title": title,
                "content": "body",
                "author_email": "ava@acme.com",
                "readers": ["ava@acme.com"],
                "created": "2026-03-01T09:00:00Z",
            }
            for doc_id, title in [("literal", "from: foo"), ("other", "foo")]
        ],
    )
    tokens = yaml.safe_load(settings.tokens_path.read_text(encoding="utf-8"))
    with client_for(settings, reload=True) as c:
        r = c.get(
            f"/gmail/v1/users/me/{surface}",
            headers={"Authorization": f"Bearer {tok(tokens, 'ava@acme.com')}"},
            params={"q": query},
        )
    assert r.status_code == 200
    assert {item["id"] for item in r.json().get(surface, [])} == {
        served_id("gmail", doc_id) for doc_id in expected
    }


@pytest.mark.parametrize("surface", ["messages", "threads"])
@pytest.mark.parametrize(
    "query, expected",
    [
        ("from:no-reply", {"joined", "hyphen"}),
        ("from:noreply", {"joined"}),
        ("from:jt-snowball", {"domain"}),
        ("from:jtsnowball", {"domain"}),
        ("from:no-repl", set()),
        ("from:noreplyx", set()),
        ("from:no-reply@bank.example", {"hyphen"}),
        ("from:no-reply@shop.example", set()),
        ("to:no-reply", {"joined", "hyphen"}),
        ("to:noreply", {"joined"}),
        ("cc:no-reply", {"joined", "hyphen"}),
        ("bcc:no-reply", {"joined", "hyphen"}),
        ("from:no--reply", {"hyphen"}),
        ("from:jt-snowball.com", set()),
        ("from:@jt-snowball", {"domain"}),
        ('from:"no-reply"', {"hyphen"}),
        ('to:"no-reply"', {"hyphen"}),
        ('cc:"no-reply"', {"hyphen"}),
        ('bcc:"no-reply"', {"hyphen"}),
        ('from:"noreply"', {"joined"}),
        ('from:"jt-snowball"', set()),
        ('from:"@jt-snowball"', set()),
        ('-from:"no-reply"', {"joined", "domain"}),
        ('-to:"no-reply"', {"joined", "domain"}),
        ('-cc:"no-reply"', {"joined", "domain"}),
        ('-from:"jt-snowball"', {"joined", "hyphen", "domain"}),
    ],
)
def test_gmail_hyphenated_address_search(tmp_path, surface, query, expected):
    """Each row is how `_gmail_address_match` reads a value with a hyphen or without one, quoted or
    not, and how a `-` in front negates it."""
    records = [
        {"doc_id": "joined", "author_email": "noreply@shop.example"},
        {"doc_id": "hyphen", "author_email": "no-reply@bank.example"},
        {"doc_id": "domain", "author_email": "news@jtsnowball.com"},
    ]
    settings = tiny_corpus(
        tmp_path,
        [
            {
                "source_type": "gmail",
                "mailbox": "ava",
                "title": "Notice",
                "content": "body",
                "to": r["author_email"],
                "cc": r["author_email"],
                "bcc": r["author_email"],
                "readers": ["ava@acme.com"],
                "visibility": "public",
                "created": "2026-03-01T09:00:00Z",
                **r,
            }
            for r in records
        ],
    )
    tokens = yaml.safe_load(settings.tokens_path.read_text(encoding="utf-8"))
    headers = {"Authorization": f"Bearer {tok(tokens, 'ava@acme.com')}"}
    with client_for(settings, reload=True) as c:
        r = c.get(f"/gmail/v1/users/me/{surface}", headers=headers, params={"q": query})
    assert r.status_code == 200
    assert {x["id"] for x in r.json().get(surface, [])} == {
        served_id("gmail", doc_id) for doc_id in expected
    }
