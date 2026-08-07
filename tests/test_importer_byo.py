"""backlot.importer.byo: load an arbitrary BYO JSONL corpus -> DB, honoring per-doc ACL."""

import gzip
import hashlib
import io
import json
import sqlite3
from pathlib import Path

import pytest
import yaml

from backlot import store
from backlot.acl import Acl
from backlot.config import Settings, get_settings
from backlot.routers.slack import _message
from backlot.importer import byo
from backlot.importer.byo import load


def _write(tmp_path, records):
    p = tmp_path / "corpus.jsonl"
    p.write_text("\n".join(json.dumps(r) for r in records))
    return p


def _dump_tables(path) -> dict[str, list]:
    """Every user table as sorted row tuples, so two DBs can be compared table by table."""
    conn = sqlite3.connect(path)
    try:
        tables = [
            r[0]
            for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' "
                "AND name NOT LIKE 'docs_fts%' AND name NOT LIKE 'sqlite_%' ORDER BY name"
            )
        ]
        return {
            t: sorted((tuple(r) for r in conn.execute(f"SELECT * FROM {t}")), key=repr)
            for t in tables
        }
    finally:
        conn.close()


def test_load_records_builds_the_same_db_as_load_from_a_file(tmp_path):
    """The record-source seam has to be a pure refactor: the same records loaded from an
    in-memory factory and from a JSONL file must produce identical tables."""
    records = [
        {
            "source_type": "confluence",
            "doc_id": "a",
            "space": "handbook",
            "group": "eng",
            "title": "A",
            "content": "alpha",
            "author_email": "ava@acme.com",
            "visibility": "public",
            "comments": [{"content": "looks right", "author_email": "bob@acme.com"}],
        },
        {
            "source_type": "slack",
            "channel": "eng",
            "group": "eng",
            "content": "hello",
            "author_email": "bob@acme.com",
            "visibility": "public",
            "replies": [{"content": "hi back", "author_email": "ava@acme.com"}],
        },
        {
            "source_type": "linear",
            "doc_id": "l1",
            "team": "engineering",
            "group": "eng",
            "title": "Fix it",
            "content": "broken",
            "author_email": "ava@acme.com",
            "identifier": "ENG-1",
            "state": "Todo",
            "visibility": "group",
        },
    ]

    (tmp_path / "file").mkdir(parents=True, exist_ok=True)
    (tmp_path / "recs").mkdir(parents=True, exist_ok=True)

    from_file = Settings(data_dir=tmp_path / "file")
    byo.load(_write(tmp_path / "file", records), from_file)

    from_recs = Settings(data_dir=tmp_path / "recs")
    byo.load_records(lambda: enumerate(records, 1), from_recs)

    assert _dump_tables(from_file.db_path) == _dump_tables(from_recs.db_path)


def test_byo_load_and_acl(tmp_path):
    corpus = _write(
        tmp_path,
        [
            {
                "source_type": "confluence",
                "title": "Public",
                "content": "x",
                "visibility": "public",
            },
            {
                "source_type": "confluence",
                "title": "Secret",
                "content": "y",
                "space": "ppl",
                "group": "people",
                "author_email": "hana@a.com",
                "author_groups": ["people"],
                "visibility": "group",
            },
            {
                "source_type": "jira",
                "title": "Mine",
                "content": "z",
                "author_email": "bob@a.com",
                "visibility": "private",
            },
        ],
    )
    settings = Settings(data_dir=tmp_path)
    res = load(corpus, settings)
    assert res["total"] == 3

    conn = store.connect_ro(settings.db_path)
    acl = Acl.load(settings.tokens_path, settings.admin_token, settings.org_name)
    tokens = {
        u["email"]: u["token"] for u in yaml.safe_load(settings.tokens_path.read_text())["users"]
    }

    def visible_titles(token, source):
        ids = acl.visible_ids(conn, acl.resolve(token))
        return sorted(
            r["title"] for r in store.list_documents(conn, source, visible_ids=ids, limit=50)
        )

    # admin (None) sees everything
    assert sorted(r["title"] for r in store.list_documents(conn, "confluence", limit=50)) == [
        "Public",
        "Secret",
    ]
    # hana is in 'people' -> sees the group-restricted page; a non-member does not
    assert visible_titles(tokens["hana@a.com"], "confluence") == ["Public", "Secret"]
    assert visible_titles(tokens["bob@a.com"], "confluence") == ["Public"]
    # private jira doc visible only to its author
    assert visible_titles(tokens["bob@a.com"], "jira") == ["Mine"]
    assert visible_titles(tokens["hana@a.com"], "jira") == []


def test_byo_readers_and_defaults(tmp_path):
    corpus = _write(
        tmp_path,
        [
            {
                "source_type": "gmail",
                "title": "Deck",
                "content": "c",
                "author_email": "ceo@a.com",
                "readers": ["ceo@a.com", "ava@a.com"],
            },
            {
                "source_type": "slack",
                "title": "hi",
                "content": "c",
            },  # no author, no visibility -> public + dsid_ id
        ],
    )
    settings = Settings(data_dir=tmp_path)
    res = load(corpus, settings)
    # the org is derived from the corpus's dominant email domain (a.com), not the default
    assert res["org"] == "a" and res["org_domain"] == "a.com"
    conn = store.connect_ro(settings.db_path)
    acl = Acl.load(settings.tokens_path, settings.admin_token, settings.org_name)
    assert acl.org_name == "a"  # Acl.load picks up the derived org from tokens.yaml
    tokens = {
        u["email"]: u["token"] for u in yaml.safe_load(settings.tokens_path.read_text())["users"]
    }

    # explicit readers: ava can see the deck doc; a stranger cannot
    deck = conn.execute("SELECT doc_id FROM gmail_messages").fetchone()["doc_id"]
    ava_ids = acl.visible_ids(conn, acl.resolve(tokens["ava@a.com"]))
    assert store.get_document(conn, "gmail", deck, visible_ids=ava_ids) is not None
    assert store.get_document(conn, "gmail", deck, visible_ids={"nobody@a.com"}) is None
    # no-author doc got a generated dsid_ id and is org-public (any real caller's
    # visible_ids includes the org sentinel = the derived org)
    slack = conn.execute("SELECT doc_id FROM slack_messages").fetchone()["doc_id"]
    assert slack.startswith("dsid_")
    assert store.get_document(conn, "slack", slack, visible_ids={res["org"]}) is not None


def test_slack_title_optional(tmp_path):
    # slack needs no title; the other sources still require one
    load(
        _write(tmp_path, [{"source_type": "slack", "content": "deploy freeze Friday"}]),
        Settings(data_dir=tmp_path),
    )
    conn = store.connect_ro((tmp_path / "mock.sqlite"))
    assert conn.execute("SELECT title FROM slack_messages").fetchone()["title"] == ""

    with pytest.raises(SystemExit):
        load(
            _write(tmp_path, [{"source_type": "confluence", "content": "no title here"}]),
            Settings(data_dir=tmp_path),
        )


def _row(**kw):
    kw.setdefault("thread_id", None)
    kw.setdefault("thread_seq", 0)
    kw.setdefault("subtype", None)
    kw.setdefault("created_ts", None)
    kw.setdefault("meta", None)
    return kw


def test_byo_meta_comments_hierarchy(tmp_path):
    load(
        _write(
            tmp_path,
            [
                {
                    "source_type": "confluence",
                    "title": "Parent",
                    "content": "p",
                    "doc_id": "pg-root",
                    "labels": ["engineering"],
                },
                {
                    "source_type": "confluence",
                    "title": "Child",
                    "content": "c",
                    "doc_id": "pg-child",
                    "parent": "pg-root",
                    "comments": [{"content": "looks good", "author_email": "rev@a.com"}],
                },
                {
                    "source_type": "jira",
                    "title": "Bug",
                    "content": "b",
                    "meta": {"issuelinks": [{"key": "X-1"}]},
                    "comments": [{"content": "fixed in main", "author_email": "dev@a.com"}],
                },
            ],
        ),
        Settings(data_dir=tmp_path),
    )
    conn = store.connect_ro(tmp_path / "mock.sqlite")

    # meta blob on a doc
    assert store.jcol(store.get_document(conn, "confluence", "pg-root"), "labels") == [
        "engineering"
    ]
    # parent/child hierarchy
    kids = store.children(conn, "confluence", "pg-root")
    assert [k["doc_id"] for k in kids] == ["pg-child"]
    # comments attached to a doc
    cs = store.doc_comments(conn, "confluence", "pg-child")
    assert len(cs) == 1 and cs[0]["body"] == "looks good"
    # jira meta + comments
    bug = conn.execute("SELECT * FROM jira_issues").fetchone()
    assert store.jcol(bug, "issuelinks")[0]["key"] == "X-1"
    assert len(store.doc_comments(conn, "jira", bug["doc_id"])) == 1


def test_slack_message_text_without_title():
    # empty title -> the message text is just the content (no bold lead line)
    assert (
        _message(_row(doc_id="d1", title="", content="hi", author_email="a@x.com"))["text"] == "hi"
    )
    assert (
        _message(_row(doc_id="d2", title="T", content="hi", author_email="a@x.com"))["text"]
        == "*T*\nhi"
    )
    # a standalone message has no thread_ts / reply_count
    assert "thread_ts" not in _message(
        _row(doc_id="d1", title="", content="hi", author_email="a@x.com")
    )


def test_byo_slack_threads(tmp_path):
    load(
        _write(
            tmp_path,
            [
                {
                    "source_type": "slack",
                    "content": "seeing 502s?",
                    "channel": "incidents",
                    "author_email": "bob@a.com",
                    "replies": [
                        {"content": "looking", "author_email": "ava@a.com"},
                        {"content": "rolled back", "author_email": "bob@a.com"},
                    ],
                }
            ],
        ),
        Settings(data_dir=tmp_path),
    )
    conn = store.connect_ro(tmp_path / "mock.sqlite")

    # 3 docs total (root + 2 replies), but only the root is top-level
    assert conn.execute("SELECT COUNT(*) FROM slack_messages").fetchone()[0] == 3
    tops = store.list_slack_top_level(conn, "incidents", limit=50)
    assert len(tops) == 1
    root = tops[0]
    assert store.slack_reply_count(conn, root["doc_id"]) == 2

    thread = store.slack_thread(conn, root["doc_id"])
    assert [r["thread_seq"] for r in thread] == [0, 1, 2]
    # replies share the root's thread_ts and sort strictly after it
    from backlot.routers.slack import _msg_ts

    ts = [_msg_ts(r) for r in thread]
    assert ts == sorted(ts) and ts[0] < ts[1] < ts[2]


def _epoch(iso):
    from datetime import datetime

    return int(datetime.fromisoformat(iso.replace("Z", "+00:00")).timestamp())


def test_byo_created_updated_times(tmp_path):
    load(
        _write(
            tmp_path,
            [
                {
                    "source_type": "jira",
                    "title": "T",
                    "content": "c",
                    "doc_id": "j1",
                    "created": "2026-03-01T09:00:00Z",
                    "updated": 1740900000,
                },
                {
                    "source_type": "google_drive",
                    "title": "D",
                    "content": "c",
                    "doc_id": "d1",
                    "created": "2026-01-15T00:00:00Z",
                },
            ],
        ),
        Settings(data_dir=tmp_path),
    )
    conn = store.connect_ro(tmp_path / "mock.sqlite")

    # created accepts ISO, updated accepts epoch int — both land as epoch seconds
    j = conn.execute("SELECT created_ts, updated_ts FROM jira_issues WHERE doc_id='j1'").fetchone()
    assert j["created_ts"] == _epoch("2026-03-01T09:00:00Z")
    assert j["updated_ts"] == 1740900000

    # and reach the router response
    from starlette.requests import Request
    from backlot.routers.atlassian import _jira_issue

    req = Request(
        {
            "type": "http",
            "headers": [],
            "query_string": b"",
            "scheme": "http",
            "server": ("t", 80),
            "path": "/",
        }
    )
    fields = _jira_issue(conn, req, store.get_document(conn, "jira", "j1"))["fields"]
    assert fields["created"].startswith("2026-03-01T09:00:00")
    assert fields["updated"].startswith("2025-03-02")  # 1740900000 -> 2025-03-02

    # updated defaults to created + 1h when omitted (drive)
    d = conn.execute("SELECT created_ts, updated_ts FROM gdrive_files WHERE doc_id='d1'").fetchone()
    assert d["created_ts"] == _epoch("2026-01-15T00:00:00Z") and d["updated_ts"] is None


def test_byo_gmail_created_and_to(tmp_path):
    load(
        _write(
            tmp_path,
            [
                {
                    "source_type": "gmail",
                    "title": "Hi",
                    "content": "body",
                    "doc_id": "m1",
                    "mailbox": "ceo",
                    "to": "board@acme.com",
                    "created": "2026-04-01T12:00:00Z",
                },
            ],
        ),
        Settings(data_dir=tmp_path),
    )
    conn = store.connect_ro(tmp_path / "mock.sqlite")
    from backlot.routers.google import _gmail_message

    msg = _gmail_message(store.get_document(conn, "gmail", "m1"), "metadata")
    assert msg["internalDate"] == str(_epoch("2026-04-01T12:00:00Z") * 1000)
    to = next(h["value"] for h in msg["payload"]["headers"] if h["name"] == "To")
    assert to == "board@acme.com"


def test_byo_slack_rich_replies(tmp_path):
    load(
        _write(
            tmp_path,
            [
                {
                    "source_type": "slack",
                    "content": "root",
                    "channel": "incidents",
                    "doc_id": "s-root",
                    "author_email": "bob@a.com",
                    "created": "2026-05-01T00:00:00Z",
                    "replies": [
                        {
                            "content": "on it",
                            "author_email": "ava@a.com",
                            "reactions": [{"name": "eyes", "count": 1, "users": ["U1"]}],
                            "subtype": "thread_broadcast",
                        },
                    ],
                }
            ],
        ),
        Settings(data_dir=tmp_path),
    )
    conn = store.connect_ro(tmp_path / "mock.sqlite")
    from backlot.routers.slack import _message, _msg_ts

    thread = store.slack_thread(conn, "s-root")
    root, reply = thread[0], thread[1]
    # root ts reflects the caller-supplied created; reply follows one second later
    assert _msg_ts(root) == f"{_epoch('2026-05-01T00:00:00Z')}.{_msg_ts(root).split('.')[1]}"
    assert _msg_ts(reply) > _msg_ts(root)
    # reply carries the full message fields (reactions + subtype), not just content
    rm = _message(reply)
    assert rm["reactions"][0]["name"] == "eyes" and rm["subtype"] == "thread_broadcast"
    # reply shares the root's thread_ts
    assert rm["thread_ts"] == _message(root, reply_count=1)["thread_ts"] == _msg_ts(root)


def test_notion_byo_load(tmp_path):
    corpus = _write(
        tmp_path,
        [
            {
                "source_type": "notion",
                "teamspace": "eng",
                "title": "Runbook",
                "content": "# Heading\n\nBody line.",
                "doc_id": "n-page",
                "author_email": "ava@acme.com",
                "visibility": "public",
                "icon": "🚀",
                "comments": [{"content": "nit", "author_email": "bob@acme.com"}],
            },
            {
                "source_type": "notion",
                "teamspace": "eng",
                "subtype": "database",
                "title": "Tasks",
                "content": "Task tracker",
                "doc_id": "n-db",
                "author_email": "ava@acme.com",
                "visibility": "public",
                "properties": {"Status": {"type": "select"}},
            },
            {
                "source_type": "notion",
                "teamspace": "eng",
                "title": "Fix gateway",
                "content": "row body",
                "doc_id": "n-row",
                "parent": "n-db",
                "author_email": "ava@acme.com",
                "visibility": "public",
                "properties": {"Status": "In Progress"},
            },
        ],
    )
    settings = Settings(data_dir=tmp_path)
    res = load(corpus, settings)
    assert res["counts"]["notion"] == 3

    conn = store.connect_ro(settings.db_path)
    row = store.get_document(conn, "notion", "n-row")
    assert row["parent_id"] == "n-db" and row["teamspace"] == "eng"
    assert '"Status"' in row["properties"]
    db = store.get_document(conn, "notion", "n-db")
    assert db["subtype"] == "database"
    page = store.get_document(conn, "notion", "n-page")
    assert page["icon"] == "🚀"
    assert len(store.doc_comments(conn, "notion", "n-page")) == 1
    assert store.get_container(conn, "notion", "eng") is not None
    conn.close()


def test_notion_byo_rejects_bad_subtype():
    from backlot.validation import record_errors

    errs = record_errors({"source_type": "notion", "title": "x", "content": "y", "subtype": "wiki"})
    assert any("subtype" in e for e in errs)


def test_s3_byo_load(tmp_path):
    unicode_body = (
        "résumé ☕ dashboards"  # multibyte: size is the UTF-8 byte length, not char count
    )
    records = [
        {
            "source_type": "s3",
            "bucket": "eng-artifacts",
            "key": "runbooks/oncall.md",
            "title": "On-call Runbook",
            "content": "check dashboards, roll back, page on-call",
            "content_type": "text/markdown",
            "author_email": "ava@acme.com",
            "author_groups": ["engineering"],
            "visibility": "public",
        },
        {
            "source_type": "s3",
            "bucket": "eng-artifacts",
            "key": "secret/comp.txt",
            "title": "Comp",
            "content": "confidential",
            "author_email": "hana@acme.com",
            "author_groups": ["people"],
            "visibility": "group",
            "group": "people",
        },
        {
            "source_type": "s3",
            "bucket": "eng-artifacts",
            "key": "notes/unicode.md",
            "title": "Unicode",
            "content": unicode_body,
            "content_type": "text/markdown",
            "author_email": "ava@acme.com",
            "author_groups": ["engineering"],
            "visibility": "public",
        },
    ]
    corpus = tmp_path / "s3.jsonl"
    corpus.write_text("\n".join(json.dumps(r) for r in records))
    settings = Settings(data_dir=tmp_path)
    res = load(corpus, settings)
    assert res["counts"]["s3"] == 3
    conn = store.connect_ro(settings.db_path)
    rows = {r["key"]: r for r in store.list_documents(conn, "s3", container="eng-artifacts")}
    assert rows["runbooks/oncall.md"]["content_type"] == "text/markdown"
    assert rows["runbooks/oncall.md"]["size"] == len("check dashboards, roll back, page on-call")
    # size is the UTF-8 byte length, which is strictly greater than the character count here
    assert rows["notes/unicode.md"]["size"] == len(unicode_body.encode("utf-8"))
    assert rows["notes/unicode.md"]["size"] != len(unicode_body)
    assert store.get_container(conn, "s3", "eng-artifacts") is not None
    conn.close()


def test_github_file_byo_load(tmp_path, monkeypatch):
    monkeypatch.setenv("BACKLOT_DATA_DIR", str(tmp_path))
    from backlot.config import get_settings

    get_settings.cache_clear()
    s = get_settings()
    p = tmp_path / "c.jsonl"
    p.write_text(
        json.dumps(
            {
                "source_type": "github",
                "subtype": "file",
                "repo": "gateway",
                "path": "src/rl/bucket.go",
                "title": "bucket.go",
                "content": "package rl\n",
                "group": "eng",
                "visibility": "group",
                "author_email": "a@acme.com",
            }
        )
    )
    byo.load(p, s, reset=True)
    conn = store.connect_ro(s.db_path)
    row = store.get_repo_file(conn, "gateway", "src/rl/bucket.go")
    assert row is not None and row["kind"] == "file" and row["content"] == "package rl\n"
    conn.close()


def test_github_file_byo_requires_path(tmp_path):
    # a file record without `path` must be rejected by schema validation
    from backlot.validation import record_errors

    errs = record_errors(
        {"source_type": "github", "subtype": "file", "title": "x", "content": "y", "repo": "r"}
    )
    assert errs  # missing path -> invalid


def test_s3_byo_rejects_missing_key(tmp_path):
    corpus = tmp_path / "bad.jsonl"
    corpus.write_text(
        json.dumps({"source_type": "s3", "bucket": "b", "title": "t", "content": "c"})
    )  # no key
    with pytest.raises(SystemExit):
        load(corpus, Settings(data_dir=tmp_path))


def _corpus(tmp_path, name, lines):
    p = tmp_path / name
    p.write_text("\n".join(json.dumps(x) for x in lines))
    return p


def _hubspot_corpus(tmp_path):
    return _write(
        tmp_path,
        [
            {
                "source_type": "hubspot",
                "object_type": "companies",
                "doc_id": "hs-co1",
                "title": "Acme Health",
                "content": "Acme Health — mid-market healthcare provider.",
                "author_email": "rep@acme.com",
                "author_groups": ["sales"],
                "visibility": "public",
                "properties": {"name": "Acme Health", "domain": "acme-health.com"},
            },
            {
                "source_type": "hubspot",
                "object_type": "contacts",
                "doc_id": "hs-c1",
                "title": "Ava Stone",
                "content": "Ava Stone — VP Platform at Acme Health.",
                "author_email": "rep@acme.com",
                "author_groups": ["sales"],
                "visibility": "public",
                "properties": {
                    "firstname": "Ava",
                    "lastname": "Stone",
                    "email": "ava@acme-health.com",
                },
                "associations": [{"to": "hs-co1", "to_type": "companies", "label": "Primary"}],
            },
            {
                "source_type": "hubspot",
                "object_type": "deals",
                "doc_id": "hs-d1",
                "title": "Acme renewal",
                "content": "Renewal for Acme Health, 12 months.",
                "author_email": "rep@acme.com",
                "author_groups": ["sales"],
                "visibility": "public",
                "properties": {
                    "dealname": "Acme renewal",
                    "amount": "50000",
                    "dealstage": "contractsent",
                },
                "associations": [{"to": "hs-co1"}],
            },  # to_type omitted -> inferred from the target
        ],
    )


def test_hubspot_byo_load(tmp_path):
    settings = Settings(data_dir=tmp_path)
    res = load(_hubspot_corpus(tmp_path), settings)
    assert res["counts"]["hubspot"] == 3
    conn = store.connect_ro(settings.db_path)
    # the object type is the grouping unit, so it scopes the listing and is registered as a container
    rows = {r["doc_id"]: r for r in store.list_documents(conn, "hubspot", container="contacts")}
    assert list(rows) == ["hs-c1"]
    assert store.jcol(rows["hs-c1"], "properties")["email"] == "ava@acme-health.com"
    assert store.get_container(conn, "hubspot", "contacts") is not None
    conn.close()


def test_hubspot_byo_associations_are_bidirectional(tmp_path):
    """A corpus declares a link once; real HubSpot exposes it from both records, so the loader
    materialises the reverse direction rather than making every author write it twice."""
    settings = Settings(data_dir=tmp_path)
    load(_hubspot_corpus(tmp_path), settings)
    conn = store.connect_ro(settings.db_path)
    # declared direction: contact -> company
    assert [r["to_doc_id"] for r in store.hubspot_associations(conn, "hs-c1", "companies")] == [
        "hs-co1"
    ]
    # reverse direction, never written by the corpus: company -> contacts
    assert [r["to_doc_id"] for r in store.hubspot_associations(conn, "hs-co1", "contacts")] == [
        "hs-c1"
    ]
    conn.close()


def test_hubspot_byo_association_infers_missing_target_type(tmp_path):
    settings = Settings(data_dir=tmp_path)
    load(_hubspot_corpus(tmp_path), settings)
    conn = store.connect_ro(settings.db_path)
    # hs-d1 declared {"to": "hs-co1"} with no to_type; the target's own object_type supplies it
    assert [r["to_doc_id"] for r in store.hubspot_associations(conn, "hs-d1", "companies")] == [
        "hs-co1"
    ]
    conn.close()


def test_append_preserves_prior_roster_and_org(tmp_path, monkeypatch):
    monkeypatch.setenv("BACKLOT_DATA_DIR", str(tmp_path))
    from backlot.config import get_settings

    get_settings.cache_clear()
    s = get_settings()
    a = _corpus(
        tmp_path,
        "a.jsonl",
        [
            {
                "source_type": "confluence",
                "title": "A",
                "content": "alpha",
                "space": "ENG",
                "author_email": "ann@acme.com",
                "visibility": "group",
                "group": "eng",
            }
        ],
    )
    byo.load(a, s, reset=True)
    prev_users = {u["email"] for u in yaml.safe_load(s.tokens_path.read_text())["users"]}
    prev_org = yaml.safe_load(s.tokens_path.read_text())["org"]
    ann_token = {
        u["email"]: u["token"] for u in yaml.safe_load(s.tokens_path.read_text())["users"]
    }["ann@acme.com"]

    # b has both a group-scoped doc (redwoodinference author, for the roster union check) and
    # a *public* doc (also redwoodinference) — a public doc gets granted to the org principal,
    # so this is what exercises the `principals WHERE type='org'` DB lookup on append: if that
    # lookup were removed and the org were re-inferred from b alone, the public doc would be
    # granted to a *new* redwoodinference org principal instead of the original acme org.
    b = _corpus(
        tmp_path,
        "b.jsonl",
        [
            {
                "source_type": "notion",
                "title": "B",
                "content": "beta rotate",
                "teamspace": "ops",
                "author_email": "bob@redwoodinference.com",
                "visibility": "group",
                "group": "ops",
            },
            {
                "source_type": "notion",
                "title": "Bpub",
                "content": "beta public",
                "teamspace": "ops",
                "author_email": "cara@redwoodinference.com",
                "visibility": "public",
            },
        ],
    )
    byo.load(b, s, reset=False)
    tok = yaml.safe_load(s.tokens_path.read_text())
    now_users = {u["email"] for u in tok["users"]}
    assert "ann@acme.com" in now_users and "bob@redwoodinference.com" in now_users  # union
    assert prev_users <= now_users
    assert tok["org"] == prev_org  # org unchanged

    conn = store.connect_ro(s.db_path)
    try:
        # exactly one org principal exists — no re-inferred second org was created
        assert conn.execute("SELECT COUNT(*) FROM principals WHERE type='org'").fetchone()[0] == 1
        # every org-scoped grant references the ORIGINAL org, proving the public doc in b
        # was granted to it rather than to a freshly re-inferred org
        org_grant_principals = {
            r[0]
            for r in conn.execute(
                "SELECT DISTINCT principal_id FROM doc_acl WHERE principal_type='org'"
            )
        }
        assert org_grant_principals == {prev_org}
    finally:
        conn.close()

    # a prior user's token is stable across the append
    assert (
        tok["users"][[u["email"] for u in tok["users"]].index("ann@acme.com")]["token"] == ann_token
    )


def test_append_incremental_fts_finds_new_and_keeps_old(tmp_path, monkeypatch):
    monkeypatch.setenv("BACKLOT_DATA_DIR", str(tmp_path))
    from backlot.config import get_settings

    get_settings.cache_clear()
    s = get_settings()
    byo.load(
        _corpus(
            tmp_path,
            "a.jsonl",
            [
                {
                    "source_type": "confluence",
                    "title": "A",
                    "content": "alpha unique",
                    "space": "ENG",
                    "author_email": "ann@acme.com",
                    "visibility": "group",
                    "group": "eng",
                }
            ],
        ),
        s,
        reset=True,
    )
    byo.load(
        _corpus(
            tmp_path,
            "b.jsonl",
            [
                {
                    "source_type": "notion",
                    "title": "B",
                    "content": "beta unique",
                    "teamspace": "ops",
                    "author_email": "bob@acme.com",
                    "visibility": "group",
                    "group": "ops",
                }
            ],
        ),
        s,
        reset=False,
    )
    conn = store.connect_ro(s.db_path)
    assert len(store.search_documents(conn, "beta", "notion")) == 1  # new doc indexed
    assert len(store.search_documents(conn, "alpha", "confluence")) == 1  # old doc still indexed
    conn.close()


# --- fireflies -------------------------------------------------------------------
# A transcript's child rows are `sentences`, NOT `replies` (which stays Slack-only), so a BYO
# author writing a transcript writes something that reads like a transcript.


def test_fireflies_byo_load_with_structured_sentences(tmp_path):
    corpus = _write(
        tmp_path,
        [
            {
                "source_type": "fireflies",
                "doc_id": "ff-1",
                "channel": "sales-calls",
                "title": "Acme discovery",
                "host_email": "ava@acme.com",
                "host_name": "Ava Chen",
                "duration": 31.5,
                "calendar_id": "cal-9",
                "created": "2026-04-02T15:00:00Z",
                "summary": {
                    "overview": "Discovery.",
                    "topics_discussed": ["latency"],
                    "action_items": ["Ava: send pricing"],
                    "meeting_type": "discovery",
                },
                "sentences": [
                    {
                        "speaker_name": "Ava Chen",
                        "author_email": "ava@acme.com",
                        "start_time": 0,
                        "text": "Let's talk latency.",
                    },
                    {"speaker_name": "Dana Ruiz", "start_time": 12, "text": "Our p95 is 300ms."},
                    {
                        "speaker_name": "Ava Chen",
                        "author_email": "ava@acme.com",
                        "start_time": 25,
                        "text": "Understood.",
                    },
                ],
            },
        ],
    )
    settings = Settings(data_dir=tmp_path)
    assert load(corpus, settings)["counts"]["fireflies"] == 1
    conn = store.connect_ro(settings.db_path)
    row = conn.execute("SELECT * FROM fireflies_transcripts").fetchone()
    assert row["channel"] == "sales-calls"
    assert row["author_email"] == "ava@acme.com"  # host_email is the author alias
    assert row["owner_display"] == "Ava Chen"
    assert row["duration"] == 31.5
    assert row["calendar_id"] == "cal-9"
    # content is DERIVED from the sentences, so it is never a second copy that can drift
    assert row["content"] == (
        "Ava Chen: Let's talk latency.\nDana Ruiz: Our p95 is 300ms.\nAva Chen: Understood."
    )
    sents = conn.execute("SELECT * FROM fireflies_sentences ORDER BY seq").fetchall()
    assert [s["speaker_name"] for s in sents] == ["Ava Chen", "Dana Ruiz", "Ava Chen"]
    assert [s["speaker_id"] for s in sents] == [0, 1, 0]  # ordinals reuse per speaker
    assert [s["author_email"] for s in sents] == ["ava@acme.com", None, "ava@acme.com"]
    assert [s["start_time"] for s in sents] == [0.0, 12.0, 25.0]
    assert all(s["end_time"] > s["start_time"] for s in sents)
    # derived where the record was silent
    assert row["transcript_id"] and row["transcript_url"].endswith(row["transcript_id"])
    assert row["audio_url"] and row["video_url"] and row["meeting_link"]
    assert row["calendar_type"] == "google_calendar"
    assert json.loads(row["analytics"])["sentiments"]["positive_pct"] is not None
    assert json.loads(row["participants"]) == ["ava@acme.com"]


def test_fireflies_byo_parses_sentences_out_of_a_plain_body(tmp_path):
    """A record with only `content` still gets per-sentence rows, so an author can write a plain
    "Speaker: text" transcript. The un-prefixed line folds into the sentence above it."""
    corpus = _write(
        tmp_path,
        [
            {
                "source_type": "fireflies",
                "doc_id": "ff-2",
                "channel": "all-hands",
                "title": "All hands",
                "author_email": "hana@acme.com",
                "content": "[00:00] Hana: numbers first.\n"
                "[00:30] Mia: design shipped selects.\n"
                "And cleared the backlog.\n"
                "[01:00] Hana: that's a wrap.",
            },
        ],
    )
    settings = Settings(data_dir=tmp_path)
    load(corpus, settings)
    conn = store.connect_ro(settings.db_path)
    sents = conn.execute("SELECT * FROM fireflies_sentences ORDER BY seq").fetchall()
    assert [s["speaker_name"] for s in sents] == ["Hana", "Mia", "Hana"]
    assert sents[1]["body"] == "design shipped selects.\nAnd cleared the backlog."
    assert [s["start_time"] for s in sents] == [0.0, 30.0, 60.0]


def test_fireflies_byo_content_and_sentences_always_round_trip(tmp_path):
    """The invariant that makes `content` a safe definition rather than a duplicate, checked for
    both the supplied-sentences and the parsed-body path."""
    from backlot import synth

    corpus = _write(
        tmp_path,
        [
            {
                "source_type": "fireflies",
                "doc_id": "ff-a",
                "title": "Given sentences",
                "sentences": [
                    {"speaker_name": "A", "text": "one"},
                    {"speaker_name": None, "text": "(crosstalk)"},
                    {"speaker_name": "B", "text": "two"},
                ],
            },
            {
                "source_type": "fireflies",
                "doc_id": "ff-b",
                "title": "Parsed body",
                "content": "[00:00] A: one.\n[00:05] B: two.",
            },
        ],
    )
    settings = Settings(data_dir=tmp_path)
    load(corpus, settings)
    conn = store.connect_ro(settings.db_path)
    for row in conn.execute("SELECT doc_id, content FROM fireflies_transcripts"):
        stored = [
            {"speaker_name": s["speaker_name"], "text": s["body"]}
            for s in conn.execute(
                "SELECT speaker_name, body FROM fireflies_sentences WHERE doc_id=? ORDER BY seq",
                (row["doc_id"],),
            )
        ]
        assert synth.fireflies_transcript_text(stored) == row["content"], row["doc_id"]
    # a null-speaker sentence renders bare, so an empty "Speaker: " prefix never enters the text
    assert (
        conn.execute("SELECT content FROM fireflies_transcripts WHERE doc_id='ff-a'").fetchone()[0]
        == "A: one\n(crosstalk)\nB: two"
    )


def test_fireflies_byo_sentences_sit_on_the_meeting_clock(tmp_path):
    corpus = _write(
        tmp_path,
        [
            {
                "source_type": "fireflies",
                "doc_id": "ff-3",
                "title": "Timed",
                "created": "2026-04-02T15:00:00Z",
                "sentences": [
                    {"speaker_name": "A", "text": "one", "start_time": 0},
                    {"speaker_name": "B", "text": "two", "start_time": 90},
                ],
            },
        ],
    )
    settings = Settings(data_dir=tmp_path)
    load(corpus, settings)
    conn = store.connect_ro(settings.db_path)
    rows = conn.execute(
        "SELECT created_ts, start_time FROM fireflies_sentences ORDER BY seq"
    ).fetchall()
    assert [r["created_ts"] for r in rows] == [1775142000, 1775142090]


def test_fireflies_byo_replies_are_still_rejected(tmp_path):
    """`replies` stays Slack-only; a transcript's child rows are `sentences`. The schema is what
    enforces it, so the mistake is caught at validation rather than silently dropped."""
    corpus = _write(
        tmp_path,
        [
            {
                "source_type": "fireflies",
                "title": "Wrong array",
                "content": "A: hi",
                "replies": [{"content": "nope"}],
            },
        ],
    )
    with pytest.raises(SystemExit) as e:
        load(corpus, Settings(data_dir=tmp_path))
    assert "replies" in str(e.value)


def test_fireflies_byo_org_is_inferred_from_host_email_and_sentence_authors(tmp_path):
    """`host_email` is Fireflies' own name for the author and `sentences[]` is its child-row array,
    so both have to feed org inference. Without them a fireflies-only corpus fell back to the
    DEFAULT org (`example`) while its users were @northwind.example — and since a public doc is
    granted to the ORG principal, every one of them would have been granted to an org nobody in the
    corpus belongs to."""
    corpus = _write(
        tmp_path,
        [
            {
                "source_type": "fireflies",
                "title": "A",
                "channel": "sales",
                "host_email": "dana@northwind.example",
                "sentences": [
                    {
                        "speaker_name": "Dana",
                        "author_email": "dana@northwind.example",
                        "text": "hi",
                    },
                    {"speaker_name": "Eli", "author_email": "eli@northwind.example", "text": "yo"},
                ],
            },
            {
                "source_type": "fireflies",
                "title": "B",
                "channel": "sales",
                "host_email": "eli@northwind.example",
                "sentences": [
                    {
                        "speaker_name": "Eli",
                        "author_email": "eli@northwind.example",
                        "text": "again",
                    }
                ],
            },
        ],
    )
    res = load(corpus, Settings(data_dir=tmp_path))
    assert (res["org"], res["org_domain"]) == ("northwind", "northwind.example")


def test_byo_emails_includes_every_author_alias():
    """The generator behind org inference. A new per-source author alias must be added here too."""
    from backlot.importer.byo import _emails

    rec = {
        "source_type": "fireflies",
        "host_email": "h@x.com",
        "readers": ["r@x.com"],
        "sentences": [{"author_email": "s@x.com"}, {"speaker_name": "no email"}],
        "comments": [{"author_email": "c@x.com"}],
    }
    assert set(_emails(rec)) == {"h@x.com", "r@x.com", "s@x.com", "c@x.com"}
    # author_email still wins for every other source, and both are yielded when both are present
    assert set(_emails({"author_email": "a@x.com", "host_email": "h@x.com"})) == {
        "a@x.com",
        "h@x.com",
    }


# --- gmail multi-message threads ---------------------------------------------------


def test_byo_gmail_thread_messages(tmp_path):
    """A Gmail thread is N messages sharing one thread id, each with its own sender, recipients and
    Message-ID — the shape `replies` cannot express (a reply is Slack's model, not email's)."""
    corpus = _write(
        tmp_path,
        [
            {
                "source_type": "gmail",
                "doc_id": "th-1",
                "mailbox": "ava",
                "title": "Retry storm",
                "content": "Seeing 5xx.",
                "author_email": "ava@a.com",
                "to": "ops@a.com",
                "message_id": "<a@a>",
                "created": "2026-01-04T09:00:00Z",
                "mailbox_owner": "Ava Chen",
                "messages": [
                    {
                        "content": "On it.",
                        "author_email": "bob@a.com",
                        "to": "ava@a.com",
                        "message_id": "<b@a>",
                        "created": "2026-01-04T10:00:00Z",
                    },
                    # header-only auto-ack: a real thread contains these, so an empty body is allowed
                    {"content": "", "author_email": "bot@a.com", "title": "Re: Retry storm"},
                ],
            },
        ],
    )
    settings = Settings(data_dir=tmp_path)
    load(corpus, settings)
    conn = store.connect_ro(settings.db_path)
    try:
        rows = store.gmail_thread(conn, "th-1")
        assert [r["thread_seq"] for r in rows] == [0, 1, 2]
        assert [r["doc_id"] for r in rows] == ["th-1", "th-1::m1", "th-1::m2"]
        # every message shares the ROOT's thread id — a child must not open a thread of its own
        assert {r["thread_id"] for r in rows} == {"th-1"}
        assert [r["author_email"] for r in rows] == ["ava@a.com", "bob@a.com", "bot@a.com"]
        assert [r["message_id"] for r in rows] == ["<a@a>", "<b@a>", None]
        assert rows[2]["content"] == "" and rows[2]["title"] == "Re: Retry storm"
        # a subject defaults to the thread's; a date-less message is an hour past the root
        assert rows[2]["title"] != rows[1]["title"]
        assert rows[2]["created_ts"] == rows[0]["created_ts"] + 2 * 3600
        # the mailbox owner is served as the owner, not the sender of any one message
        assert rows[0]["owner_display"] == "Ava Chen"
        # children inherit the root's ACL, or a non-admin reader sees a truncated thread
        assert store.doc_grants(conn, "th-1::m1") == store.doc_grants(conn, "th-1")
    finally:
        conn.close()


def test_byo_gmail_message_requires_the_content_key(tmp_path):
    corpus = _write(
        tmp_path,
        [{"source_type": "gmail", "title": "t", "content": "c", "messages": [{"to": "x@a.com"}]}],
    )
    with pytest.raises(SystemExit):
        load(corpus, Settings(data_dir=tmp_path))


# --- per-service people/scope fields ----------------------------------------------


def test_byo_per_service_people_and_scope_fields(tmp_path):
    """The fields an ERB import writes that BYO could not: confluence confidentiality/owner_team/
    reviewers, drive collaborators, jira severity/squad, slack participants, and the owner's
    display name on every source whose table has one."""
    corpus = _write(
        tmp_path,
        [
            {
                "source_type": "confluence",
                "doc_id": "c1",
                "space": "ENG",
                "title": "Runbook",
                "content": "x",
                "author_email": "ava@a.com",
                "author_name": "Tomás Rré",
                "confidentiality": "restricted (customer-sensitive)",
                "owner_team": "engineering",
                "reviewers": ["bob@a.com", "cara@a.com"],
            },
            {
                "source_type": "google_drive",
                "doc_id": "d1",
                "folder": "research",
                "title": "Model",
                "content": "x",
                "author_email": "ava@a.com",
                "author_name": "Ava Chen",
                "collaborators": ["bob@a.com"],
            },
            {
                "source_type": "jira",
                "doc_id": "j1",
                "project": "PAY",
                "title": "Latency",
                "content": "x",
                "author_email": "ava@a.com",
                "author_name": "Ava Chen",
                "severity": "Sev1",
                "squad": "payments-core",
            },
            {
                "source_type": "slack",
                "doc_id": "s1",
                "channel": "incidents",
                "content": "502s?",
                "author_email": "ava@a.com",
                "participants": ["ava", "bob"],
            },
        ],
    )
    settings = Settings(data_dir=tmp_path)
    load(corpus, settings)
    conn = store.connect_ro(settings.db_path)
    try:
        c = store.get_document(conn, "confluence", "c1")
        assert c["confidentiality"] == "restricted (customer-sensitive)"
        assert c["owner_team"] == "engineering"
        assert json.loads(c["reviewers"]) == ["bob@a.com", "cara@a.com"]
        # the display name is STORED: it cannot be recovered from the email (the accents are lost)
        assert c["owner_display"] == "Tomás Rré"
        d = store.get_document(conn, "google_drive", "d1")
        assert json.loads(d["collaborators"]) == ["bob@a.com"] and d["owner_display"] == "Ava Chen"
        j = store.get_document(conn, "jira", "j1")
        assert (j["severity"], j["squad"], j["owner_display"]) == (
            "Sev1",
            "payments-core",
            "Ava Chen",
        )
        s = store.get_document(conn, "slack", "s1")
        assert json.loads(s["participants"]) == ["ava", "bob"]
    finally:
        conn.close()


def test_byo_group_null_means_the_container_owns_no_group(tmp_path):
    """`"group": null` is a real state, not a missing value — a Gmail mailbox has no group scope,
    and inferring one from its name would invent a grantable principal."""
    corpus = _write(
        tmp_path,
        [
            {
                "source_type": "gmail",
                "doc_id": "g1",
                "mailbox": "ava",
                "title": "t",
                "content": "c",
                "author_email": "ava@a.com",
                "group": None,
            },
            {
                "source_type": "google_drive",
                "doc_id": "d1",
                "folder": "scratch",
                "title": "t",
                "content": "c",
                "author_email": "ava@a.com",
            },
        ],
    )
    settings = Settings(data_dir=tmp_path)
    load(corpus, settings)
    conn = store.connect_ro(settings.db_path)
    try:
        assert store.get_container(conn, "gmail", "ava")["group_id"] is None
        # an ABSENT group still falls back to the container slug
        assert store.get_container(conn, "google_drive", "scratch")["group_id"] == "scratch"
        # ...and a null group never becomes a principal
        assert conn.execute("SELECT COUNT(*) FROM principals WHERE id='ava'").fetchone()[0] == 0
    finally:
        conn.close()


def test_byo_typed_reader_principals(tmp_path):
    """`readers` can name the ORG principal, so a document that is org-readable AND names its
    owners is expressible — with the bare shorthand it was one or the other."""
    corpus = _write(
        tmp_path,
        [
            {
                "source_type": "confluence",
                "doc_id": "c1",
                "title": "t",
                "content": "c",
                "author_email": "ava@acme.com",
                "readers": ["user:ava@acme.com", "group:eng", "org:acme"],
            },
            {
                "source_type": "confluence",
                "doc_id": "c2",
                "title": "t2",
                "content": "c",
                "author_email": "ava@acme.com",
                "readers": ["ava@acme.com", "eng"],
            },
        ],
    )
    settings = Settings(data_dir=tmp_path)
    load(corpus, settings)
    conn = store.connect_ro(settings.db_path)
    try:
        assert {
            (r["principal_type"], r["principal_id"])
            for r in conn.execute("SELECT * FROM doc_acl WHERE doc_id='c1'")
        } == {("user", "ava@acme.com"), ("group", "eng"), ("org", "acme")}
        # the unprefixed shorthand is unchanged
        assert {
            (r["principal_type"], r["principal_id"])
            for r in conn.execute("SELECT * FROM doc_acl WHERE doc_id='c2'")
        } == {("user", "ava@acme.com"), ("group", "eng")}
    finally:
        conn.close()


# --- roster sidecar ---------------------------------------------------------------


def test_byo_roster_is_the_closed_principal_set(tmp_path):
    """With a roster, a record's emails REFERENCE principals rather than declaring them: only
    `departments` members get a token, `contacts` are principals without one, and an address in no
    roster (a Slack display handle) stays a plain address instead of becoming an org account."""
    roster = tmp_path / "roster.yaml"
    roster.write_text(
        yaml.safe_dump(
            {
                "org": "redwood",
                "org_domain": "redwoodinference.com",
                "departments": {
                    "Engineering": [{"name": "Ava Chen", "email": "ava.chen@redwoodinference.com"}]
                },
                "contacts": [
                    {
                        "name": "Tomás Rré",
                        "email": "tomas.rre@redwoodinference.com",
                        "group": "engineering",
                    },
                    {"name": "Zoe Newperson", "email": "zoe.newperson@redwoodinference.com"},
                ],
            }
        )
    )
    corpus = _write(
        tmp_path,
        [
            {
                "source_type": "confluence",
                "doc_id": "c1",
                "space": "ENG",
                "group": "engineering",
                "title": "t",
                "content": "c",
                "author_email": "ava.chen@redwoodinference.com",
                "readers": ["user:tomas.rre@redwoodinference.com", "org:redwood"],
            },
            # a slack display handle: never an account, even though it authors a message
            {
                "source_type": "slack",
                "doc_id": "s1",
                "channel": "incidents",
                "content": "hi",
                "author_email": "infrabot@redwoodinference.com",
            },
        ],
    )
    settings = Settings(data_dir=tmp_path)
    load(corpus, settings, roster=roster)

    tokens = yaml.safe_load(settings.tokens_path.read_text())
    assert tokens["org"] == "redwood" and tokens["org_domain"] == "redwoodinference.com"
    assert [u["email"] for u in tokens["users"]] == ["ava.chen@redwoodinference.com"]

    conn = store.connect_ro(settings.db_path)
    try:
        people = {
            r["id"]: r["display_name"]
            for r in conn.execute("SELECT id, display_name FROM principals WHERE type='user'")
        }
        assert set(people) == {
            "ava.chen@redwoodinference.com",
            "tomas.rre@redwoodinference.com",
            "zoe.newperson@redwoodinference.com",
        }
        # the roster's names win over anything derivable from the address
        assert people["tomas.rre@redwoodinference.com"] == "Tomás Rré"
        # the slack handle authored a row but is not a principal
        assert "infrabot@redwoodinference.com" not in people
        # membership comes from the roster, not from the containers a user wrote in
        assert {
            (r["group_id"], r["user_id"]) for r in conn.execute("SELECT * FROM group_members")
        } == {
            ("engineering", "ava.chen@redwoodinference.com"),
            ("engineering", "tomas.rre@redwoodinference.com"),
        }
        assert {r["id"] for r in conn.execute("SELECT id FROM principals WHERE type='group'")} == {
            "engineering"
        }
    finally:
        conn.close()


def test_byo_roster_departments_alone_is_an_employee_directory(tmp_path):
    """The bench's `employee_directory.yaml` is usable as a roster verbatim, which is what lets a
    converted corpus ship the directory it was resolved against."""
    directory = tmp_path / "employee_directory.yaml"
    directory.write_text(
        yaml.safe_dump(
            {
                "departments": {
                    "Research & Applied ML": [
                        {"name": "Maya Chen", "email": "maya.chen@r.com", "title": "RS"}
                    ]
                }
            }
        )
    )
    corpus = _write(
        tmp_path,
        [
            {
                "source_type": "jira",
                "doc_id": "j1",
                "project": "PAY",
                "title": "t",
                "content": "c",
                "author_email": "maya.chen@r.com",
                "visibility": "group",
                "group": "research-applied-ml",
            }
        ],
    )
    settings = Settings(data_dir=tmp_path)
    load(corpus, settings, roster=directory)
    conn = store.connect_ro(settings.db_path)
    try:
        # the department name becomes its group id via slugify, as Principals does
        assert {
            (r["group_id"], r["user_id"]) for r in conn.execute("SELECT * FROM group_members")
        } == {("research-applied-ml", "maya.chen@r.com")}
    finally:
        conn.close()


def test_byo_jsonl_records_split_only_on_newline(tmp_path):
    """A record whose text contains U+2028 is one record. `str.splitlines()` breaks on it, which
    tore a valid line into two invalid halves — one such character appears in the bench corpus."""
    body = "before\u2028after"  # U+2028 LINE SEPARATOR, inside a JSON string
    corpus = tmp_path / "corpus.jsonl"
    # ensure_ascii=False, so the character reaches the file raw rather than as an escape
    corpus.write_text(
        "\n".join(
            json.dumps(r, ensure_ascii=False)
            for r in [
                {"source_type": "confluence", "doc_id": "c1", "title": "t", "content": body},
                {"source_type": "confluence", "doc_id": "c2", "title": "t2", "content": "second"},
            ]
        ),
        encoding="utf-8",
    )
    # guards the test itself: without a character splitlines() breaks on, it proves nothing
    assert len(corpus.read_text().splitlines()) == 3

    from backlot.validation import validate_file

    assert validate_file(corpus) == []
    settings = Settings(data_dir=tmp_path)
    assert load(corpus, settings)["counts"] == {"confluence": 2}
    conn = store.connect_ro(settings.db_path)
    try:
        assert store.get_document(conn, "confluence", "c1")["content"] == body
    finally:
        conn.close()


def test_byo_gmail_messages_join_the_root_s_declared_thread(tmp_path):
    """A record may open a thread under an explicit `thread` id that is not its doc_id. Its
    messages have to land in THAT thread — keying them off the root's doc_id instead split one
    conversation into two."""
    corpus = _write(
        tmp_path,
        [
            {
                "source_type": "gmail",
                "doc_id": "gm-1",
                "thread": "gm-deck",
                "mailbox": "ceo",
                "title": "Deck",
                "content": "draft",
                "author_email": "ceo@a.com",
                "messages": [{"content": "reviewed", "author_email": "ava@a.com"}],
            }
        ],
    )
    settings = Settings(data_dir=tmp_path)
    load(corpus, settings)
    conn = store.connect_ro(settings.db_path)
    try:
        rows = store.gmail_thread(conn, "gm-deck")
        assert [(r["doc_id"], r["thread_seq"]) for r in rows] == [("gm-1", 0), ("gm-1::m1", 1)]
        assert {r["thread_id"] for r in rows} == {"gm-deck"}
    finally:
        conn.close()


def test_byo_comment_times_are_monotonic_across_a_mixed_thread(tmp_path):
    """A thread that mixes dated and undated comments must stay in order. `created + position`
    lands an undated comment back at the DOCUMENT's creation time, so a dated one written earlier
    in the array sorts after it — and `Issue.comments` orders by createdAt, so the thread is served
    inverted. This is the rule `erb.load_linear` already applied."""
    corpus = _write(
        tmp_path,
        [
            {
                "source_type": "linear",
                "doc_id": "ln-1",
                "team": "engineering",
                "title": "t",
                "content": "c",
                "author_email": "ava@a.com",
                "created": "2026-02-08T09:00:00Z",
                "comments": [
                    {"content": "first, dated later", "created_ts": "2026-02-09T10:00:00Z"},
                    {"content": "second, undated"},
                    {"content": "third, dated later still", "created_ts": "2026-02-11T08:00:00Z"},
                    {"content": "fourth, undated"},
                ],
            },
        ],
    )
    settings = Settings(data_dir=tmp_path)
    load(corpus, settings)
    conn = store.connect_ro(settings.db_path)
    try:
        rows = store.doc_comments(conn, "linear", "ln-1")
        times = [r["created_ts"] for r in rows]
        assert times == sorted(times), f"comments out of order: {times}"
        # the undated one follows its predecessor rather than jumping back to the doc's clock
        assert times[1] == times[0] + 1 and times[3] == times[2] + 1
    finally:
        conn.close()


def test_byo_all_undated_comments_keep_the_doc_clock_plus_position(tmp_path):
    """The monotonic rule must not change the ordinary case."""
    corpus = _write(
        tmp_path,
        [
            {
                "source_type": "jira",
                "doc_id": "j-1",
                "project": "PAY",
                "title": "t",
                "content": "c",
                "author_email": "ava@a.com",
                "created": 1_700_000_000,
                "comments": [{"content": "one"}, {"content": "two"}, {"content": "three"}],
            },
        ],
    )
    settings = Settings(data_dir=tmp_path)
    load(corpus, settings)
    conn = store.connect_ro(settings.db_path)
    try:
        assert [r["created_ts"] for r in store.doc_comments(conn, "jira", "j-1")] == [
            1_700_000_001,
            1_700_000_002,
            1_700_000_003,
        ]
    finally:
        conn.close()


def test_byo_empty_readers_means_nobody(tmp_path):
    """`"readers": []` is the only way to say "admin-only", and it has to mean that: falling
    through to the public default would make the most restrictive spelling produce the least
    restrictive result. An ABSENT `readers` is still public."""
    corpus = _write(
        tmp_path,
        [
            {
                "source_type": "gmail",
                "doc_id": "gm-dark",
                "mailbox": "inbox",
                "title": "t",
                "content": "c",
                "readers": [],
            },
            {
                "source_type": "gmail",
                "doc_id": "gm-open",
                "mailbox": "inbox",
                "title": "t2",
                "content": "c",
            },
        ],
    )
    settings = Settings(data_dir=tmp_path)
    load(corpus, settings)
    conn = store.connect_ro(settings.db_path)
    try:
        assert store.doc_grants(conn, "gm-dark") == []
        assert store.doc_grants(conn, "gm-open")  # absent readers -> the org default
        # ...so it is invisible to a user token and reachable only by admin (visible_ids=None)
        assert store.get_document(conn, "gmail", "gm-dark", visible_ids={"acme"}) is None
        assert store.get_document(conn, "gmail", "gm-dark") is not None
    finally:
        conn.close()


def _shard_artifact(tmp_path):
    """A two-shard artifact plus the manifest that describes it, written the way `export_byo` does."""
    import gzip as _gz
    import hashlib as _hl
    import io as _io
    import json as _js

    rows = [
        {
            "source_type": "confluence",
            "space": "handbook",
            "title": "Onboarding",
            "content": "How we onboard.",
            "author_email": "ava@acme.com",
        },
        {
            "source_type": "slack",
            "channel": "incidents",
            "content": "502s from the gateway?",
            "author_email": "bob@acme.com",
        },
    ]
    out = tmp_path / "artifact"
    sources = {}
    for rec in rows:
        src = rec["source_type"]
        d = out / "data" / src
        d.mkdir(parents=True, exist_ok=True)
        p = d / "part-00000.jsonl.gz"
        with _io.TextIOWrapper(_gz.GzipFile(p, "wb", mtime=0), encoding="utf-8") as fh:
            fh.write(_js.dumps(rec) + "\n")
        sources[src] = {
            "documents": 1,
            "records": 1,
            "shards": [
                {
                    "path": str(p.relative_to(out)),
                    "records": 1,
                    "bytes": p.stat().st_size,
                    "sha256": _hl.sha256(p.read_bytes()).hexdigest(),
                }
            ],
        }
    (out / "manifest.json").write_text(
        _js.dumps(
            {
                "schema": 1,
                "documents": len(rows),
                "records": len(rows),
                "shard_records": 1,
                "sources": sources,
            }
        )
    )
    return out


def test_load_reads_a_sharded_artifact_as_one_corpus(tmp_path):
    """A sharded directory has to load exactly like the single file it was split from — the corpus
    is too large to hold in memory, so `load` streams it shard by shard through the manifest."""
    out = _shard_artifact(tmp_path)
    data = tmp_path / "data"
    data.mkdir()
    settings = Settings(data_dir=data)
    res = byo.load(out, settings)
    assert res["total"] == 2
    assert res["counts"] == {"confluence": 1, "slack": 1}
    # line numbers run across the whole artifact, so a report names one place
    assert [n for n, _ in byo.corpus_records(out)] == [1, 2]


def test_verify_manifest_catches_a_tampered_shard(tmp_path):
    """The digest is the whole point: a truncated or swapped download must fail before it loads."""
    out = _shard_artifact(tmp_path)
    assert byo.verify_manifest(out) == []
    shard = next(out.glob("data/*/part-00000.jsonl.gz"))
    good = shard.read_bytes()
    shard.write_bytes(good + b"\x00")
    problems = byo.verify_manifest(out)
    assert len(problems) == 1 and "bytes" in problems[0]

    # A swap that keeps the byte count is what the digest is FOR. The size check above answers the
    # other cases and returns early, so without this the sha256 comparison never runs in the suite.
    shard.write_bytes(good[:-1] + bytes([good[-1] ^ 0xFF]))
    assert shard.stat().st_size == len(good)
    problems = byo.verify_manifest(out)
    assert len(problems) == 1 and "sha256 mismatch" in problems[0]


def test_verify_manifest_checks_the_roster_too(tmp_path):
    """The roster is the closed principal set — it decides who holds a token and what they can see —
    and importing a directory picks it up without being asked, so a swapped one must not pass."""
    out = _shard_artifact(tmp_path)
    roster = out / "roster.yaml"
    roster.write_text(
        yaml.safe_dump(
            {
                "org": "Acme",
                "org_domain": "acme.com",
                "departments": {"eng": [{"email": "ava@acme.com"}]},
            }
        )
    )
    mf = out / "manifest.json"
    manifest = json.loads(mf.read_text())
    manifest["roster"] = {
        "path": "roster.yaml",
        "bytes": roster.stat().st_size,
        "sha256": hashlib.sha256(roster.read_bytes()).hexdigest(),
    }
    mf.write_text(json.dumps(manifest))
    assert byo.verify_manifest(out) == []

    roster.write_text(roster.read_text() + "\n# swapped for another org's\n")
    problems = byo.verify_manifest(out)
    assert any("roster.yaml" in p for p in problems), problems


def test_import_refuses_a_shard_that_does_not_match_the_manifest(tmp_path, monkeypatch):
    """A shard that is short but validly terminated is what a resumed download looks like. Rewriting
    one to fewer records leaves a well-formed gzip stream, so nothing downstream notices: the import
    used to report success on a corpus missing documents the manifest counts."""
    out = _shard_artifact(tmp_path)
    shard = next(out.glob("data/*/part-00000.jsonl.gz"))
    with gzip.open(shard, "rt", encoding="utf-8") as fh:
        kept = fh.readlines()[:-1]
    with io.TextIOWrapper(gzip.GzipFile(shard, "wb", mtime=0), encoding="utf-8") as fh:
        fh.writelines(kept)

    data = tmp_path / "data-refused"
    monkeypatch.setenv("BACKLOT_DATA_DIR", str(data))
    get_settings.cache_clear()
    with pytest.raises(SystemExit) as e:
        byo.main([str(out)])
    assert e.value.code == 1
    assert not (data / "mock.sqlite").exists(), "a rejected artifact must not leave a database"


def test_a_single_gzipped_corpus_file_loads(tmp_path):
    """The README documents `python -m backlot.importer.byo corpus.jsonl.gz`; every other test reaches a
    `.gz` through a shard directory, so the plain single-file case had no coverage."""
    corpus = tmp_path / "corpus.jsonl.gz"
    with io.TextIOWrapper(gzip.GzipFile(corpus, "wb", mtime=0), encoding="utf-8") as fh:
        fh.write(
            json.dumps(
                {
                    "source_type": "slack",
                    "channel": "general",
                    "author_email": "ava@acme.com",
                    "content": "Gzipped.",
                }
            )
            + "\n"
        )
    settings = Settings(data_dir=tmp_path / "d")
    res = byo.load(corpus, settings)
    assert res["total"] == 1
    conn = store.connect_ro(settings.db_path)
    assert conn.execute("SELECT content FROM slack_messages").fetchone()[0] == "Gzipped."
    conn.close()


def test_a_directory_without_a_manifest_is_refused_clearly(tmp_path):
    """It is the same situation `verify_manifest` names, so it should not surface as a traceback."""
    empty = tmp_path / "not-an-artifact"
    empty.mkdir()
    with pytest.raises(SystemExit) as e:
        list(byo.corpus_records(empty))
    assert "manifest.json" in str(e.value)


def test_a_manifest_naming_a_shard_outside_the_artifact_is_refused(tmp_path):
    """The artifact is downloaded, so its manifest is untrusted input."""
    out = _shard_artifact(tmp_path)
    mf = out / "manifest.json"
    manifest = json.loads(mf.read_text())
    src = sorted(manifest["sources"])[0]
    manifest["sources"][src]["shards"][0]["path"] = "../escaped.jsonl.gz"
    mf.write_text(json.dumps(manifest))
    with pytest.raises(SystemExit) as e:
        list(byo.corpus_records(out))
    assert "outside" in str(e.value)


def test_two_sources_may_share_a_doc_id(tmp_path):
    """Ids are per service, not corpus-wide. The bench has three documents that appear under two
    sources with the same `dataset_doc_uuid` (a drive file that is also a confluence page, plus a
    hubspot and a jira one), and a direct ERB import keeps both because each source is its own
    table. Deduping across the whole corpus dropped whichever source sorted later, which is what
    made the full round-trip diverge: gdrive_files 25,108 vs 25,107."""
    corpus = tmp_path / "shared-id.jsonl"
    corpus.write_text(
        "\n".join(
            json.dumps(r)
            for r in [
                {
                    "source_type": "confluence",
                    "space": "handbook",
                    "doc_id": "shared-1",
                    "title": "Sprint plan (page)",
                    "content": "The confluence rendering.",
                    "author_email": "ava@acme.com",
                },
                {
                    "source_type": "google_drive",
                    "folder": "users",
                    "doc_id": "shared-1",
                    "title": "Sprint plan (doc)",
                    "content": "The drive document.",
                    "author_email": "ava@acme.com",
                },
            ]
        )
        + "\n"
    )
    data = tmp_path / "data"
    data.mkdir()
    settings = Settings(data_dir=data)
    res = byo.load(corpus, settings)
    assert res["total"] == 2
    conn = store.connect_ro(settings.db_path)
    assert (
        conn.execute("SELECT title FROM confluence_pages WHERE doc_id='shared-1'").fetchone()[0]
        == "Sprint plan (page)"
    )
    assert (
        conn.execute("SELECT title FROM gdrive_files WHERE doc_id='shared-1'").fetchone()[0]
        == "Sprint plan (doc)"
    )


def test_load_records_source_documents_counts_documents_not_rows(tmp_path):
    """One Slack record with two replies is 1 source document and 3 message rows."""
    from backlot import store
    from tests._helpers import build_corpus

    settings = build_corpus(
        tmp_path,
        [
            {
                "source_type": "slack",
                "channel": "incidents",
                "author_email": "bob@acme.com",
                "content": "Anyone seeing 502s from the gateway?",
                "replies": [
                    {"content": "Looking now.", "author_email": "ava@acme.com"},
                    {"content": "Rolled back.", "author_email": "bob@acme.com"},
                ],
            },
        ],
    )
    conn = store.connect_ro(settings.db_path)
    assert store.read_meta(conn, "source_documents") == "1"
    rows = conn.execute(f"SELECT COUNT(*) FROM {store.table('slack')}").fetchone()[0]
    assert rows == 3
    conn.close()


def test_load_records_source_documents_sums_across_sources(tmp_path):
    from backlot import store
    from tests._helpers import build_corpus

    settings = build_corpus(
        tmp_path,
        [
            {
                "source_type": "confluence",
                "space": "handbook",
                "title": "Handbook",
                "content": "How we build software.",
                "author_email": "ava@acme.com",
            },
            {
                "source_type": "gmail",
                "mailbox": "ceo",
                "title": "Q1 deck",
                "content": "Draft narrative.",
                "author_email": "ceo@acme.com",
                "to": "ava@acme.com",
            },
        ],
    )
    conn = store.connect_ro(settings.db_path)
    assert store.read_meta(conn, "source_documents") == "2"
    conn.close()


def test_append_accumulates_source_documents(tmp_path):
    """reset=False appends, so the count adds rather than replaces."""
    import json
    from backlot import store
    from backlot.config import Settings
    from backlot.importer.byo import load

    settings = Settings(data_dir=tmp_path)
    first = tmp_path / "a.jsonl"
    first.write_text(
        json.dumps(
            {
                "source_type": "confluence",
                "space": "h",
                "title": "A",
                "content": "a",
                "author_email": "ava@acme.com",
            }
        )
    )
    second = tmp_path / "b.jsonl"
    second.write_text(
        json.dumps(
            {
                "source_type": "confluence",
                "space": "h",
                "title": "B",
                "content": "b",
                "author_email": "ava@acme.com",
            }
        )
    )
    load(first, settings)
    load(second, settings, reset=False)
    conn = store.connect_ro(settings.db_path)
    assert store.read_meta(conn, "source_documents") == "2"
    conn.close()


def test_hello_corpus_loads_and_covers_every_source(tmp_path):
    """The wheel's built-in corpus must load and exercise all ten sources it covers.

    Ten, not eleven: the hello-world corpus deliberately omits fireflies.
    """
    hello_sources = (
        "slack",
        "gmail",
        "google_drive",
        "github",
        "jira",
        "confluence",
        "notion",
        "linear",
        "hubspot",
        "s3",
    )
    hello = Path(__file__).resolve().parent.parent / "backlot" / "data" / "hello.jsonl"
    settings = Settings(data_dir=tmp_path)
    load(hello, settings)
    conn = store.connect_ro(settings.db_path)
    counts = {
        src: conn.execute(f"SELECT COUNT(*) FROM {store.table(src)}").fetchone()[0]
        for src in hello_sources
    }
    for src, n in counts.items():
        assert n > 0, f"hello corpus has no {src} rows"
    # The two counts must differ, or the corpus does not demonstrate the parsing layer.
    assert int(store.read_meta(conn, "source_documents")) < sum(counts.values())
    conn.close()
