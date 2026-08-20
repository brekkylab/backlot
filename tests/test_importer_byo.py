"""backlot.importer.byo: load an arbitrary BYO JSONL corpus -> DB, honoring per-doc ACL."""

import gzip
import hashlib
import io
import json
import sqlite3
from pathlib import Path

import pytest
import yaml

from backlot import store, synth
from tests._helpers import complete, served_id
from backlot.acl import Acl
from backlot.config import Settings, get_settings
from backlot.routers.slack import _message
from backlot.importer import byo
from backlot.importer.byo import load


def _write(tmp_path, records, name="corpus.jsonl", *, raw=False):
    """A corpus file. Each record is completed against its schema first, so a test states only the
    fields it is about; ``raw=True`` writes them as given, for the tests about refusal itself."""
    if not raw:
        records = [
            complete(**r) if isinstance(r, dict) and "source_type" in r else r for r in records
        ]
    p = tmp_path / name
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
                "AND name NOT LIKE '%_fts' AND name NOT LIKE '%_fts_%' AND name NOT LIKE 'sqlite_%' ORDER BY name"
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
        complete(
            **{
                "source_type": "confluence",
                "doc_id": "a",
                "space": "handbook",
                "group": "eng",
                "title": "A",
                "content": "alpha",
                "author_email": "ava@acme.com",
                "visibility": "public",
                "comments": [{"content": "looks right", "author_email": "bob@acme.com"}],
            }
        ),
        complete(
            **{
                "source_type": "slack",
                "channel": "eng",
                "group": "eng",
                "content": "hello",
                "author_email": "bob@acme.com",
                "visibility": "public",
                "replies": [{"content": "hi back", "author_email": "ava@acme.com"}],
            }
        ),
        complete(
            **{
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
            }
        ),
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
    deck = conn.execute("SELECT id FROM gmail_messages").fetchone()["id"]
    ava_ids = acl.visible_ids(conn, acl.resolve(tokens["ava@a.com"]))
    assert store.get_document(conn, "gmail", deck, visible_ids=ava_ids) is not None
    assert store.get_document(conn, "gmail", deck, visible_ids={"nobody@a.com"}) is None
    # no-author doc got a generated dsid_ id and is org-public (any real caller's
    # visible_ids includes the org sentinel = the derived org)
    slack = tuple(conn.execute("SELECT channel, ts FROM slack_messages").fetchone())
    assert store.get_document(conn, "slack", *slack, visible_ids={res["org"]}) is not None


def _row(**kw):
    kw.setdefault("channel", "inc")
    kw.setdefault("thread_ts", None)
    kw.setdefault("thread_seq", 0)
    kw.setdefault("subtype", None)
    kw.setdefault("created_ts", None)
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
                    "issuelinks": [{"key": "X-1"}],
                    "comments": [{"content": "fixed in main", "author_email": "dev@a.com"}],
                },
            ],
        ),
        Settings(data_dir=tmp_path),
    )
    conn = store.connect_ro(tmp_path / "mock.sqlite")

    # meta blob on a doc
    assert store.jcol(
        store.get_document(conn, "confluence", served_id("confluence", "pg-root")), "labels"
    ) == ["engineering"]
    # parent/child hierarchy
    kids = store.children(conn, "confluence", served_id("confluence", "pg-root"))
    assert [k["id"] for k in kids] == [served_id("confluence", "pg-child")]
    # comments attached to a doc
    cs = store.doc_comments(conn, "confluence", served_id("confluence", "pg-child"))
    assert len(cs) == 1 and cs[0]["body"] == "looks good"
    # jira meta + comments
    bug = conn.execute("SELECT * FROM jira_issues").fetchone()
    assert store.jcol(bug, "issuelinks")[0]["key"] == "X-1"
    assert len(store.doc_comments(conn, "jira", bug["key"])) == 1


def test_slack_message_text_is_the_content():
    assert _message(_row(ts="1.0", content="hi", author_email="a@x.com"))["text"] == "hi"
    # a standalone message has no thread_ts / reply_count
    assert "thread_ts" not in _message(_row(ts="1.0", content="hi", author_email="a@x.com"))


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
    assert store.slack_reply_count(conn, root["channel"], root["ts"]) == 2

    thread = store.slack_thread(conn, root["channel"], root["ts"])
    assert [r["thread_seq"] for r in thread] == [0, 1, 2]
    # replies share the root's thread_ts and sort strictly after it
    ts = [r["ts"] for r in thread]
    assert ts == sorted(ts) and ts[0] < ts[1] < ts[2]


def _epoch(iso):
    from datetime import datetime

    return int(datetime.fromisoformat(iso.replace("Z", "+00:00")).timestamp())


def test_byo_created_updated_times(tmp_path):
    load(
        _write(
            tmp_path,
            [
                complete(
                    **{
                        "source_type": "jira",
                        "title": "T",
                        "content": "c",
                        "doc_id": "j1",
                        "created": "2026-03-01T09:00:00Z",
                        "updated": 1740900000,
                    }
                ),
                complete(
                    **{
                        "source_type": "google_drive",
                        "title": "D",
                        "content": "c",
                        "doc_id": "d1",
                        "created": "2026-01-15T00:00:00Z",
                    }
                ),
            ],
        ),
        Settings(data_dir=tmp_path),
    )
    conn = store.connect_ro(tmp_path / "mock.sqlite")

    # created accepts ISO, updated accepts epoch int — both land as epoch seconds
    j = conn.execute("SELECT created_ts, updated_ts FROM jira_issues").fetchone()
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
    fields = _jira_issue(conn, req, conn.execute("SELECT * FROM jira_issues").fetchone())["fields"]
    assert fields["created"].startswith("2026-03-01T09:00:00")
    assert fields["updated"].startswith("2025-03-02")  # 1740900000 -> 2025-03-02

    # updated defaults to created + 1h when omitted (drive)
    d = conn.execute(
        "SELECT created_ts, updated_ts FROM gdrive_files WHERE id = ?",
        (served_id("google_drive", "d1"),),
    ).fetchone()
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

    msg = _gmail_message(store.get_document(conn, "gmail", served_id("gmail", "m1")), "metadata")
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
    from backlot.routers.slack import _message

    channel, root_ts = conn.execute(
        "SELECT channel, ts FROM slack_messages WHERE thread_seq = 0"
    ).fetchone()
    thread = store.slack_thread(conn, channel, root_ts)
    root, reply = thread[0], thread[1]
    # root ts reflects the caller-supplied created; reply follows one second later
    assert root["ts"] == f"{_epoch('2026-05-01T00:00:00Z')}.{root['ts'].split('.')[1]}"
    assert reply["ts"] > root["ts"]
    # reply carries the full message fields (reactions + subtype), not just content
    rm = _message(reply)
    assert rm["reactions"][0]["name"] == "eyes" and rm["subtype"] == "thread_broadcast"
    # reply shares the root's thread_ts
    assert rm["thread_ts"] == _message(root, reply_count=1)["thread_ts"] == root["ts"]


def test_byo_slack_reply_carries_its_own_clock(tmp_path):
    """A reply's `created` is honored when the corpus writes one — the treatment a
    gmail message already gets — and an absent one lands a second after the message
    before it, which is exactly where root+position always put it. Every reply of a
    clockless thread therefore loads byte-identically to the old rule."""
    load(
        _write(
            tmp_path,
            [
                complete(
                    **{
                        "source_type": "slack",
                        "content": "root",
                        "channel": "incidents",
                        "doc_id": "s-root",
                        "author_email": "bob@a.com",
                        "created": "2026-05-01T00:00:00Z",
                        "replies": [
                            {"content": "quick ack", "author_email": "ava@a.com"},
                            {
                                "content": "the real answer, hours later",
                                "author_email": "ava@a.com",
                                "created": "2026-05-01T03:00:00Z",
                            },
                            {"content": "thanks", "author_email": "bob@a.com"},
                        ],
                    }
                )
            ],
        ),
        Settings(data_dir=tmp_path),
    )
    conn = store.connect_ro(tmp_path / "mock.sqlite")
    thread = conn.execute(
        "SELECT ts, created_ts FROM slack_messages WHERE channel = 'incidents' ORDER BY thread_seq"
    ).fetchall()
    base = _epoch("2026-05-01T00:00:00Z")
    assert [r["created_ts"] for r in thread] == [
        base,
        base + 1,  # clockless: one second after the root
        base + 3 * 3600,  # its own clock
        base + 3 * 3600 + 1,  # clockless: one second after the clocked reply
    ]
    ts = [r["ts"] for r in thread]
    assert ts == sorted(ts) and len(set(ts)) == 4


def test_byo_slack_clockless_root_is_grounded_on_its_replies(tmp_path):
    """A root with no `created` of its own holds a second hashed from its doc_id, which
    is not a fact about the thread. Left as the ordering anchor it made the import turn
    on that hash — the same corpus loading or dying depending on the root's doc_id — and
    when it loaded it served a root years before its own reply. It is re-grounded on the
    first reply that carries a clock, so every doc_id resolves the same way."""
    ids = [f"s-root-{n}" for n in range(12)]
    load(
        _write(
            tmp_path,
            [
                complete(
                    **{
                        "source_type": "slack",
                        "content": "root",
                        "channel": "incidents",
                        "doc_id": did,
                        "author_email": "bob@a.com",
                        "replies": [
                            {"content": "quick ack", "author_email": "ava@a.com"},
                            {
                                "content": "the real answer",
                                "author_email": "ava@a.com",
                                "created": "2024-06-01T00:00:00Z",
                            },
                        ],
                    }
                )
                for did in ids
            ],
        ),
        Settings(data_dir=tmp_path),
    )
    conn = store.connect_ro(tmp_path / "mock.sqlite")
    base = _epoch("2024-06-01T00:00:00Z")
    threads: dict = {}
    for r in conn.execute(
        "SELECT thread_ts, created_ts FROM slack_messages ORDER BY thread_ts, thread_seq"
    ):
        threads.setdefault(r["thread_ts"], []).append(r["created_ts"])
    assert len(threads) == len(ids)  # every doc_id, not just the ones whose hash sorts early
    for secs in threads.values():
        assert secs == [
            base - 2,  # the root, one second ahead of the clockless reply
            base - 1,  # clockless: one second before the clock it is grounded on
            base,
        ]


def test_byo_slack_clockless_thread_ignores_the_regrounding(tmp_path):
    """A thread that supplies no clock anywhere keeps the root's synthesized second and
    lands every reply where root+position always put it — there is nothing to re-ground
    on, and the whole point of the default is that such a corpus loads unchanged."""
    load(
        _write(
            tmp_path,
            [
                complete(
                    **{
                        "source_type": "slack",
                        "content": "root",
                        "channel": "incidents",
                        "doc_id": "s-mute",
                        "author_email": "bob@a.com",
                        "replies": [
                            {"content": "one", "author_email": "ava@a.com"},
                            {"content": "two", "author_email": "ava@a.com"},
                        ],
                    }
                )
            ],
        ),
        Settings(data_dir=tmp_path),
    )
    conn = store.connect_ro(tmp_path / "mock.sqlite")
    base = synth.epoch("s-mute")
    assert [
        r["created_ts"]
        for r in conn.execute("SELECT created_ts FROM slack_messages ORDER BY thread_seq")
    ] == [base, base + 1, base + 2]


def test_byo_slack_reply_clock_refusal_owns_up_to_a_defaulted_second(tmp_path):
    """When an explicit clock collides with a second this importer chose rather than one
    the author wrote, the error says so. Two adjacent seconds cannot both hold a message
    — a Slack ts is identity as well as clock — so the refusal stands, but quoting the
    defaulted second as though the corpus had supplied it sent authors hunting for a
    value that is nowhere in their file."""
    corpus = _write(
        tmp_path,
        [
            complete(
                **{
                    "source_type": "slack",
                    "content": "Anyone else seeing 502s?",
                    "channel": "incidents",
                    "author_email": "bob@a.com",
                    "created": "2026-02-10T18:00:00Z",
                    "replies": [
                        {"content": "on it", "author_email": "ava@a.com"},
                        {
                            "content": "the real answer",
                            "author_email": "ava@a.com",
                            "created": "2026-02-10T18:00:01Z",
                        },
                    ],
                }
            )
        ],
    )
    with pytest.raises(SystemExit, match="reply 1 carries no created of its own"):
        load(corpus, Settings(data_dir=tmp_path))


def test_byo_slack_reply_clock_must_move_forward(tmp_path):
    """A Slack ts is identity as well as clock: two thread messages sharing a second
    would collide at the ts every endpoint resolves, and a reply at or before its
    parent is a shape real Slack cannot emit. Refused at import, loudly."""
    corpus = _write(
        tmp_path,
        [
            {
                "source_type": "slack",
                "content": "root",
                "channel": "incidents",
                "author_email": "bob@a.com",
                "created": "2026-05-01T02:00:00Z",
                "replies": [
                    {
                        "content": "from the past",
                        "author_email": "ava@a.com",
                        "created": "2026-05-01T01:00:00Z",
                    }
                ],
            }
        ],
    )
    with pytest.raises(SystemExit, match="after the message before it"):
        load(corpus, Settings(data_dir=tmp_path))


@pytest.mark.parametrize(
    "bad",
    [
        "2026-05-01 03:00 PM",  # a time, in a format nothing here reads
        "2026-13-01T00:00:00Z",  # month 13
        "yesterday",
    ],
)
def test_byo_slack_reply_clock_must_be_readable(tmp_path, bad):
    """A filled-in `created` the importer cannot read is refused, not defaulted. Taking
    the default would silently reinstate the metronome the field exists to replace, and
    land a second indistinguishable from what a corpus that wrote no clock at all gets."""
    corpus = _write(
        tmp_path,
        [
            {
                "source_type": "slack",
                "content": "root",
                "channel": "incidents",
                "author_email": "bob@a.com",
                "created": "2026-05-01T02:00:00Z",
                "replies": [{"content": "typo", "author_email": "ava@a.com", "created": bad}],
            }
        ],
    )
    with pytest.raises(SystemExit, match="not a time this importer can read"):
        load(corpus, Settings(data_dir=tmp_path))


def test_byo_record_level_clocks_refuse_an_unreadable_value(tmp_path):
    """The asymmetry a reply's refusal created: a root's own `created` took the
    synthesized `epoch(doc_id)` for a typo without a word, so a record whose author
    wrote a real date loaded with a hash of its id instead and read as one that had
    been left blank. Every time field whose absence gets a default in its place now
    refuses an unreadable value, across source types."""
    base = {
        "source_type": "confluence",
        "title": "T",
        "content": "c",
        "space": "handbook",
        "author_email": "b@a.com",
        "visibility": "public",
    }
    cases = [
        ("created", {**base, "doc_id": "c1", "created": "2026-13-01T00:00:00Z"}),
        ("updated", {**base, "doc_id": "c2", "updated": "2026-05-01 03:00 PM"}),
        (
            "created_ts",
            {
                **base,
                "doc_id": "c3",
                "comments": [{"content": "hi", "author_email": "a@a.com", "created_ts": "nope"}],
            },
        ),
        (
            "created",
            {
                "source_type": "gmail",
                "title": "S",
                "content": "c",
                "doc_id": "g1",
                "author_email": "b@a.com",
                "visibility": "public",
                "messages": [
                    {"content": "m", "author_email": "a@a.com", "created": "2026-13-01T00:00:00Z"}
                ],
            },
        ),
        # the numeric door: json.loads reads a bare Infinity as a float
        ("created", {**base, "doc_id": "c4", "created": float("inf")}),
    ]
    for field, rec in cases:
        corpus = _write(tmp_path, [rec])
        with pytest.raises(SystemExit, match=f"{field} is not a time this importer can read"):
            load(corpus, Settings(data_dir=tmp_path))


def test_byo_absent_clocks_still_take_their_defaults(tmp_path):
    """Refusing an unreadable time must not refuse an absent one. A record with no
    `created` keeps `epoch(doc_id)`; `updated` left out stays NULL; an undated comment
    follows the one before it."""
    load(
        _write(
            tmp_path,
            [
                complete(
                    **{
                        "source_type": "confluence",
                        "title": "T",
                        "content": "c",
                        "doc_id": "cf-bare",
                        "space": "handbook",
                        "author_email": "b@a.com",
                        "visibility": "public",
                        "comments": [{"content": "hi", "author_email": "a@a.com"}],
                    }
                )
            ],
        ),
        Settings(data_dir=tmp_path),
    )
    conn = store.connect_ro(tmp_path / "mock.sqlite")
    row = conn.execute("SELECT created_ts, updated_ts FROM confluence_pages").fetchone()
    assert row["created_ts"] == synth.epoch("cf-bare")
    assert row["updated_ts"] is None
    c = conn.execute("SELECT created_ts FROM confluence_comments").fetchone()
    assert c["created_ts"] == synth.epoch("cf-bare") + 1


def test_byo_a_stated_epoch_zero_is_a_second_not_a_missing_value(tmp_path):
    """1970-01-01T00:00:00Z parses to 0, which is falsy. Every field whose ABSENCE takes a default
    had to test for absence rather than truthiness, or a second the author wrote is replaced by
    one nobody did — the confusion `_epoch_field` exists to prevent, reintroduced by the `or`
    that consumed its answer."""
    load(
        _write(
            tmp_path,
            [
                {
                    "source_type": "confluence",
                    "title": "T",
                    "content": "c",
                    "doc_id": "cf-zero",
                    "space": "handbook",
                    "author_email": "b@a.com",
                    "visibility": "public",
                    "created": 1000,
                    "comments": [{"content": "hi", "author_email": "a@a.com", "created_ts": 0}],
                },
                {
                    "source_type": "gmail",
                    "title": "S",
                    "content": "c",
                    "doc_id": "gm-zero",
                    "author_email": "b@a.com",
                    "visibility": "public",
                    "created": 1000,
                    "messages": [{"content": "m", "author_email": "a@a.com", "created": 0}],
                },
            ],
        ),
        Settings(data_dir=tmp_path),
    )
    conn = store.connect_ro(tmp_path / "mock.sqlite")
    assert conn.execute("SELECT created_ts FROM confluence_comments").fetchone()[0] == 0
    assert conn.execute("SELECT created_ts FROM gmail_messages").fetchone()[0] == 0


@pytest.mark.parametrize(
    "bad",
    [
        "inf",
        "-inf",
        "Infinity",
        "1e400",  # overflows to inf
        "1e309",
        "nan",
        "1e18",  # finite, but past the last year datetime can render
        "1770746760000",  # milliseconds where seconds belong
    ],
)
def test_byo_reply_clock_refuses_unservable_numbers(tmp_path, bad):
    """A number with no usable second refuses the import like any other unreadable
    value. `int()` raises rather than returning for inf and nan, and a finite second
    past year 9999 imports clean and then raises when a router dates the row — so
    neither may reach the database, and neither may escape as a traceback."""
    corpus = _write(
        tmp_path,
        [
            {
                "source_type": "slack",
                "content": "root",
                "channel": "incidents",
                "author_email": "bob@a.com",
                "created": "2026-05-01T02:00:00Z",
                "replies": [{"content": "x", "author_email": "ava@a.com", "created": bad}],
            }
        ],
    )
    with pytest.raises(SystemExit, match="not a time this importer can read"):
        load(corpus, Settings(data_dir=tmp_path))


def test_byo_epoch_handles_every_number_without_raising(tmp_path):
    """`_epoch` answers None rather than raising, for every shape a corpus can put in a
    time field. `json.loads` accepts bare `Infinity` and `NaN`, so these arrive as floats
    as well as strings, and an uncaught `OverflowError` there came out of `load()` as a
    traceback — the outcome the refusal exists to replace."""
    from backlot.importer.byo import _epoch

    for v in ["inf", "-inf", "Infinity", "-Infinity", "1e400", "1e309", "nan", "1e18"]:
        assert _epoch(v) is None, v
    for v in [float("inf"), float("-inf"), float("nan"), 1e18, 1770746760000]:
        assert _epoch(v) is None, v
    # the seconds a corpus legitimately writes still parse, including 0 and pre-1970
    assert _epoch(0) == 0 and _epoch("0") == 0
    assert _epoch("-86400") == -86400
    assert _epoch("1770746760") == 1770746760


def test_byo_epoch_seconds_as_a_string(tmp_path):
    """Epoch seconds written as a string are read, including the `<sec>.<frac>` form a
    Slack ts takes — the shape an `edited.ts` in the same record is already in, and the
    natural thing to write next to it. ISO is still tried first, so an 8-digit
    basic-format date stays a 2026 date rather than becoming a 1970 second."""
    import datetime as dt

    from backlot.importer.byo import _epoch

    assert _epoch("1770746760") == 1770746760
    assert _epoch("1770746760.000000") == 1770746760
    assert dt.datetime.fromtimestamp(_epoch("20260501"), dt.timezone.utc).year == 2026
    load(
        _write(
            tmp_path,
            [
                {
                    "source_type": "slack",
                    "content": "root",
                    "channel": "incidents",
                    "doc_id": "s-str",
                    "author_email": "bob@a.com",
                    "created": "1770746400",
                    "replies": [
                        {
                            "content": "six minutes later",
                            "author_email": "ava@a.com",
                            "created": "1770746760.000000",
                        }
                    ],
                }
            ],
        ),
        Settings(data_dir=tmp_path),
    )
    conn = store.connect_ro(tmp_path / "mock.sqlite")
    assert [
        r["created_ts"]
        for r in conn.execute("SELECT created_ts FROM slack_messages ORDER BY thread_seq")
    ] == [1770746400, 1770746760]


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
    row = store.get_document(conn, "notion", served_id("notion", "n-row"))
    assert row["parent_id"] == served_id("notion", "n-db") and row["teamspace"] == "eng"
    assert '"Status"' in row["properties"]
    db = store.get_document(conn, "notion", served_id("notion", "n-db"))
    assert db["subtype"] == "database"
    page = store.get_document(conn, "notion", served_id("notion", "n-page"))
    assert page["icon"] == "🚀"
    assert len(store.doc_comments(conn, "notion", served_id("notion", "n-page"))) == 1
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
    corpus.write_text("\n".join(json.dumps(complete(**r)) for r in records))
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
            complete(
                **{
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
    # As written: the missing `key` is the refusal under test.
    corpus.write_text(json.dumps(complete("s3", _omit={"key"}, bucket="b", title="t", content="c")))
    with pytest.raises(SystemExit):
        load(corpus, Settings(data_dir=tmp_path))


def _corpus(tmp_path, name, lines):
    p = tmp_path / name
    p.write_text("\n".join(json.dumps(complete(**x)) for x in lines))
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
    rows = {r["id"]: r for r in store.list_documents(conn, "hubspot", container="contacts")}
    assert list(rows) == [served_id("hubspot", "hs-c1")]
    assert (
        store.jcol(rows[served_id("hubspot", "hs-c1")], "properties")["email"]
        == "ava@acme-health.com"
    )
    assert store.get_container(conn, "hubspot", "contacts") is not None
    conn.close()


def test_hubspot_byo_associations_are_bidirectional(tmp_path):
    """A corpus declares a link once; real HubSpot exposes it from both records, so the loader
    materialises the reverse direction rather than making every author write it twice."""
    settings = Settings(data_dir=tmp_path)
    load(_hubspot_corpus(tmp_path), settings)
    conn = store.connect_ro(settings.db_path)
    # declared direction: contact -> company
    assert [
        r["to_id"]
        for r in store.hubspot_associations(conn, served_id("hubspot", "hs-c1"), "companies")
    ] == [served_id("hubspot", "hs-co1")]
    # reverse direction, never written by the corpus: company -> contacts
    assert [
        r["to_id"]
        for r in store.hubspot_associations(conn, served_id("hubspot", "hs-co1"), "contacts")
    ] == [served_id("hubspot", "hs-c1")]
    conn.close()


def test_hubspot_byo_association_infers_missing_target_type(tmp_path):
    settings = Settings(data_dir=tmp_path)
    load(_hubspot_corpus(tmp_path), settings)
    conn = store.connect_ro(settings.db_path)
    # hs-d1 declared {"to": "hs-co1"} with no to_type; the target's own object_type supplies it
    assert [
        r["to_id"]
        for r in store.hubspot_associations(conn, served_id("hubspot", "hs-d1"), "companies")
    ] == [served_id("hubspot", "hs-co1")]
    conn.close()


def test_hubspot_byo_association_to_a_ghost_target_is_an_import_error(tmp_path):
    """An explicit `to_type` names what KIND the target is (the schema's own words: "default: the
    target record's own object_type") -- it must not also be a license to link to a doc_id that
    was never written: writing the association anyway leaves `store.hubspot_associations` returning
    zero rows for it forever, with nothing said at import."""
    settings = Settings(data_dir=tmp_path)
    corpus = _write(
        tmp_path,
        [
            {
                "source_type": "hubspot",
                "object_type": "contacts",
                "doc_id": "hs-c1",
                "title": "Ava Stone",
                "content": "Ava Stone.",
                "author_email": "rep@acme.com",
                "properties": {"firstname": "Ava"},
                "associations": [{"to": "ghost", "to_type": "companies"}],
            }
        ],
    )
    with pytest.raises(SystemExit) as e:
        load(corpus, settings)
    assert "ghost" in str(e.value) and "not found" in str(e.value)


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
                f"SELECT DISTINCT principal_id FROM {store.acl_table('notion')} "
                "WHERE principal_type='org'"
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
    assert row["id"] and row["transcript_url"].endswith(row["id"])
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
    both the supplied-sentences and the parsed-body path.

    The stored sentence COUNT is part of it: a sentence the concatenation does not contain is a row
    the API serves and full-text search cannot find.
    """
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
    for row in conn.execute("SELECT id, content FROM fireflies_transcripts"):
        stored = [
            {"speaker_name": s["speaker_name"], "text": s["body"]}
            for s in conn.execute(
                "SELECT speaker_name, body FROM fireflies_sentences WHERE transcript_id = ? "
                "ORDER BY seq",
                (row["id"],),
            )
        ]
        assert synth.fireflies_transcript_text(stored) == row["content"], row["id"]
    # a null-speaker sentence renders bare, so an empty "Speaker: " prefix never enters the text
    assert (
        conn.execute(
            "SELECT content FROM fireflies_transcripts WHERE id = ?",
            (served_id("fireflies", "ff-a"),),
        ).fetchone()[0]
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


def test_fireflies_transcript_id_collision_raises_on_import(tmp_path, monkeypatch):
    """A transcript id is the table's PRIMARY KEY, so two transcripts resolving to the same one is
    a loud import failure. Left merely indexed, one of them is silently unreachable at its own id
    -- last-writer-wins with no error, which is the defect the whole identifier scheme exists to
    remove. Fireflies looks exempt because its id reads as "unique by construction"; it is not.

    A transcript's id is `synth.fireflies_id(doc_id)` when the corpus is silent, reached through
    `store.ID_SEED` like every other synthesized id — so forcing a collision between two DIFFERENT
    doc_ids collapses that registry entry to one constant. `setitem`, not `setattr`: the registry
    captured the function when `backlot.store` was imported, so rebinding the name on `synth` would
    leave the captured reference untouched."""
    docs = [
        {
            "source_type": "fireflies",
            "doc_id": f"ff-{i}",
            "channel": "sales-calls",
            "title": f"Call {i}",
            "host_email": "ava@acme.com",
            "content": "Ava: hello.",
        }
        for i in range(2)
    ]
    (tmp_path / "ok").mkdir(parents=True, exist_ok=True)
    settings = Settings(data_dir=tmp_path / "ok")
    assert load(_write(tmp_path / "ok", docs), settings)["counts"]["fireflies"] == 2  # sanity

    monkeypatch.setitem(store.ID_SEED, "fireflies", (lambda seed: "constant", None))
    (tmp_path / "collide").mkdir(parents=True, exist_ok=True)
    settings2 = Settings(data_dir=tmp_path / "collide")
    with pytest.raises(SystemExit, match="already resolves to"):
        load(_write(tmp_path / "collide", docs), settings2)


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
            complete(
                **{
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
                }
            ),
        ],
    )
    settings = Settings(data_dir=tmp_path)
    load(corpus, settings)
    conn = store.connect_ro(settings.db_path)
    try:
        rows = store.gmail_thread(conn, served_id("gmail", "th-1"))
        assert [r["thread_seq"] for r in rows] == [0, 1, 2]
        assert [r["id"] for r in rows] == [
            served_id("gmail", t) for t in ("th-1", "th-1::m1", "th-1::m2")
        ]
        # every message shares the ROOT's thread id — a child must not open a thread of its own
        assert {r["thread_id"] for r in rows} == {served_id("gmail", "th-1")}
        assert [r["author_email"] for r in rows] == ["ava@a.com", "bob@a.com", "bot@a.com"]
        assert [r["message_id"] for r in rows] == ["<a@a>", "<b@a>", None]
        assert rows[2]["content"] == "" and rows[2]["title"] == "Re: Retry storm"
        # a subject defaults to the thread's; a date-less message is an hour past the root
        assert rows[2]["title"] != rows[1]["title"]
        assert rows[2]["created_ts"] == rows[0]["created_ts"] + 2 * 3600
        # the mailbox owner is served as the owner, not the sender of any one message
        assert rows[0]["owner_display"] == "Ava Chen"
        # children inherit the root's ACL, or a non-admin reader sees a truncated thread
        assert store.doc_grants(conn, "gmail", "th-1::m1") == store.doc_grants(
            conn, "gmail", "th-1"
        )
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
        acl = store.acl_table("confluence")
        assert {
            (r["principal_type"], r["principal_id"])
            for r in conn.execute(
                f"SELECT * FROM {acl} WHERE id = ?", (served_id("confluence", "c1"),)
            )
        } == {("user", "ava@acme.com"), ("group", "eng"), ("org", "acme")}
        # the unprefixed shorthand is unchanged
        assert {
            (r["principal_type"], r["principal_id"])
            for r in conn.execute(
                f"SELECT * FROM {acl} WHERE id = ?", (served_id("confluence", "c2"),)
            )
        } == {("user", "ava@acme.com"), ("group", "eng")}
    finally:
        conn.close()


def test_byo_provided_tracker_ids_are_stored_and_served(tmp_path):
    """A corpus that writes its own issue keys and PR numbers into document text needs the API to
    serve those exact strings, or every citation a document makes dangles on the served surface.
    A provided jira `key` / github `number` is stored at load and wins. A github record without a
    number keeps the synthesized one, materialized for the same reason as Linear's `identifier`; a
    jira record without a key has one COMPOSED for it at import, under the prefix its provided
    sibling established. A github `file` row's provided number is still ignored, and the one it is
    assigned comes last — after every issue and pull has claimed its spelling — so it cannot shadow
    a real issue or PR."""
    from backlot import synth
    from tests._helpers import build_corpus, client_for

    records = [
        {
            "source_type": "jira",
            "doc_id": "j-key",
            "project": "payments",
            "title": "t",
            "content": "c",
            "key": "PAY-7",
            "author_email": "ava@acme.com",
        },
        {
            "source_type": "jira",
            "doc_id": "j-plain",
            "project": "payments",
            "title": "t2",
            "content": "c2",
            "author_email": "ava@acme.com",
        },
        {
            "source_type": "github",
            "doc_id": "g-num",
            "repo": "core",
            "subtype": "pull_request",
            "title": "t",
            "content": "c",
            "number": 4242,
            "author_email": "ava@acme.com",
        },
        {
            "source_type": "github",
            "doc_id": "g-file",
            "repo": "core",
            "subtype": "file",
            "path": "src/a.py",
            "title": "a.py",
            "content": "print()",
            "author_email": "ava@acme.com",
            # a file row's provided number is ignored — the schema says so, and honouring it
            # would let a file claim a number a real issue or PR asked for
            "number": 9,
        },
    ]
    settings = build_corpus(tmp_path, records)

    conn = store.connect_ro(settings.db_path)
    try:
        jira = {r["title"]: r["key"] for r in conn.execute("SELECT title, key FROM jira_issues")}
        assert jira["t"] == "PAY-7"
        # `key` is now the one served column, so the keyless sibling carries a COMPOSED key
        # rather than NULL -- under the prefix its provided sibling established, and not the
        # spelling that sibling claimed.
        assert jira["t2"].startswith("PAY-") and jira["t2"] != "PAY-7"
        gh = {
            r["title"]: (r["number"], r["kind"])
            for r in conn.execute("SELECT title, number, kind FROM github_items")
        }
        assert gh["t"] == (4242, "pull_request")
        # The file was assigned a number of its own — it needs one to be addressable under the
        # primary key — but NOT the 9 it asked for, and not the pull's.
        assert gh["a.py"][0] not in (9, 4242)
    finally:
        conn.close()

    tokens = yaml.safe_load(settings.tokens_path.read_text())
    hdr = {
        "Authorization": "Bearer "
        + next(u["token"] for u in tokens["users"] if u["email"] == "ava@acme.com")
    }
    with client_for(settings, reload=True) as c:
        got = c.get("/atlassian/rest/api/3/issue/PAY-7", headers=hdr)
        assert got.status_code == 200 and got.json()["key"] == "PAY-7"
        # the synthesized spelling still resolves for the record that wrote none — under the
        # prefix its project's provided key carries, which is the string the API serves for it
        plain = synth.jira_key("j-plain", "PAY")
        assert c.get(f"/atlassian/rest/api/3/issue/{plain}", headers=hdr).status_code == 200
        pull = c.get("/github/repos/acme/core/pulls/4242", headers=hdr)
        assert pull.status_code == 200 and pull.json()["number"] == 4242


def test_byo_provided_key_prefix_is_the_project_key(tmp_path):
    """An agent that reads PAY-7 out of a document navigates by PAY: the issue's
    `fields.project.key`, the project picker and JQL `project = PAY` must all speak the
    provided prefix, or the corpus cites keys its own API cannot navigate."""
    from tests._helpers import build_corpus, client_for

    settings = build_corpus(
        tmp_path,
        [
            {
                "source_type": "jira",
                "doc_id": "j-key",
                "project": "payments",
                "title": "t",
                "content": "c",
                "key": "PAY-7",
                "author_email": "ava@acme.com",
            }
        ],
    )
    tokens = yaml.safe_load(settings.tokens_path.read_text())
    hdr = {
        "Authorization": "Bearer "
        + next(u["token"] for u in tokens["users"] if u["email"] == "ava@acme.com")
    }
    with client_for(settings, reload=True) as c:
        issue = c.get("/atlassian/rest/api/3/issue/PAY-7", headers=hdr).json()
        assert issue["fields"]["project"]["key"] == "PAY"
        found = c.get(
            "/atlassian/rest/api/3/search/jql", headers=hdr, params={"jql": "project = PAY"}
        ).json()
        assert [i["key"] for i in found["issues"]] == ["PAY-7"]
        projects = c.get("/atlassian/rest/api/3/project/search", headers=hdr).json()
        assert "PAY" in [p["key"] for p in projects["values"]]


def test_byo_one_provided_key_sets_the_prefix_for_its_keyless_siblings(tmp_path):
    """A project whose issues carry a MIX of provided and absent keys is the normal case for a
    corpus that cites keys in document text: only the cited issues need to spell one out. Every
    issue in such a project must still answer at one prefix — two spellings at once (the provided
    `PAY-7` beside a sibling's `PAYMENTS<hash>-<n>`) would leave `project = PAY` returning a subset
    of its own project and the picker naming a key half the issues do not use.

    The keyless row is why the loader stores no synthesized key: whichever row sorted first by
    `doc_id` would otherwise decide the prefix, and here that row is the keyless one."""
    from backlot import synth
    from tests._helpers import build_corpus, client_for

    def issue(did):
        return complete(
            source_type="jira",
            doc_id=did,
            project="payments",
            title=did,
            content="c",
            author_email="ava@acme.com",
        )

    # 'j-aaa' sorts before 'j-zzz': the keyless row is the one the index reaches first.
    settings = build_corpus(tmp_path, [issue("j-aaa"), {**issue("j-zzz"), "key": "PAY-7"}])
    tokens = yaml.safe_load(settings.tokens_path.read_text())
    hdr = {
        "Authorization": "Bearer "
        + next(u["token"] for u in tokens["users"] if u["email"] == "ava@acme.com")
    }
    keyless = synth.jira_key("j-aaa", "PAY")
    assert keyless.startswith("PAY-")
    with client_for(settings, reload=True) as c:
        served = c.get(f"/atlassian/rest/api/3/issue/{keyless}", headers=hdr)
        assert served.status_code == 200
        assert served.json()["key"] == keyless
        assert served.json()["fields"]["project"]["key"] == "PAY"
        found = c.get(
            "/atlassian/rest/api/3/search/jql", headers=hdr, params={"jql": "project = PAY"}
        ).json()
        assert sorted(i["key"] for i in found["issues"]) == sorted([keyless, "PAY-7"])
        projects = c.get("/atlassian/rest/api/3/project/search", headers=hdr).json()
        assert [p["key"] for p in projects["values"] if p["key"].startswith("PAY")] == ["PAY"]


def test_byo_colliding_provided_id_claims_the_spelling_and_the_other_row_moves(tmp_path):
    """Provided numbers claim their spelling first, and a row whose derived number was taken
    moves to the next free one — it does not merely stay listable. Left as an alias that
    setdefault silently dropped, that row advertised a number which fetched the provider
    and was reachable at nothing: the index and the serving path disagreed about the same
    document. Both now read the number `resolve_github_numbers` assigned.

    Also covers the other half: the provider does NOT additionally answer at its own synthesized
    spelling. One column holds ONE number per row, and an issue answering at a second, unrequested
    number is not something real GitHub does either (`/issues/<not-this-issue's-number>` 404s
    there)."""
    from backlot import synth
    from tests._helpers import build_corpus, client_for

    stolen = synth.github_number("g-victim")
    settings = build_corpus(
        tmp_path,
        [
            {
                "source_type": "github",
                "doc_id": "g-victim",
                "repo": "core",
                "title": "v",
                "content": "v",
                "author_email": "ava@acme.com",
            },
            {
                "source_type": "github",
                "doc_id": "g-a-thief",
                "repo": "core",
                "subtype": "pull_request",
                "title": "t",
                "content": "t",
                "author_email": "ava@acme.com",
                "number": stolen,
            },
        ],
    )
    tokens = yaml.safe_load(settings.tokens_path.read_text())
    hdr = {
        "Authorization": "Bearer "
        + next(u["token"] for u in tokens["users"] if u["email"] == "ava@acme.com")
    }
    with client_for(settings, reload=True) as c:
        got = c.get(f"/github/repos/acme/core/pulls/{stolen}", headers=hdr).json()
        assert got["title"] == "t"  # the provided id wins the spelling
        # The provider's own synthesized spelling is NOT a second way to reach it -- a real GitHub
        # 404s an issue's own number spelled differently, and so does this.
        alias = synth.github_number("g-a-thief")
        got2 = c.get(f"/github/repos/acme/core/pulls/{alias}", headers=hdr)
        assert got2.status_code == 404
        listing = c.get(
            "/github/repos/acme/core/issues", headers=hdr, params={"state": "open"}
        ).json()
        served = {i["title"]: i["number"] for i in listing}
        assert "v" in served
        # The displaced row advertises a number that is not the provider's, and fetching
        # that number returns the displaced row itself.
        assert served["v"] != stolen
        back = c.get(f"/github/repos/acme/core/issues/{served['v']}", headers=hdr)
        assert back.status_code == 200 and back.json()["title"] == "v"


def test_byo_a_provided_number_wins_whichever_doc_id_sorts_first(tmp_path):
    """The contract above cannot depend on doc_id order, and it did: both index passes read one
    column, so a synthesized number written into it at load was indistinguishable from a provided
    one, and the provider lost its own number to a row that merely hashed to it and sorted earlier
    — a 404 at the number the corpus had asked for. The victim doc_id sorts FIRST here; only a
    corpus that stores nothing for an absent number can keep the provider's claim.

    A keyless row therefore holds a provisional number until the second assignment pass — same
    value, decided after every provided one is already registered."""
    from backlot import synth
    from tests._helpers import build_corpus, client_for

    stolen = synth.github_number("g-a-victim")
    settings = build_corpus(
        tmp_path,
        [
            {
                "source_type": "github",
                "doc_id": "g-a-victim",
                "repo": "core",
                "title": "v",
                "content": "v",
                "author_email": "ava@acme.com",
            },
            {
                "source_type": "github",
                "doc_id": "g-thief",
                "repo": "core",
                "subtype": "pull_request",
                "title": "t",
                "content": "t",
                "author_email": "ava@acme.com",
                "number": stolen,
            },
        ],
    )
    conn = store.connect_ro(settings.db_path)
    try:
        stored = {
            r["title"]: r["number"] for r in conn.execute("SELECT title, number FROM github_items")
        }
        # `number` is now the one served column, so the victim carries an assigned number
        # rather than NULL. The property under test is unchanged: the PROVIDER keeps the spelling
        # it asked for, and the row whose derived number wanted it is moved off.
        assert stored["t"] == stolen
        assert stored["v"] not in (None, stolen)
    finally:
        conn.close()

    tokens = yaml.safe_load(settings.tokens_path.read_text())
    hdr = {
        "Authorization": "Bearer "
        + next(u["token"] for u in tokens["users"] if u["email"] == "ava@acme.com")
    }
    with client_for(settings, reload=True) as c:
        got = c.get(f"/github/repos/acme/core/pulls/{stolen}", headers=hdr)
        assert got.status_code == 200 and got.json()["title"] == "t"
        assert got.json()["number"] == stolen
        listing = c.get(
            "/github/repos/acme/core/issues", headers=hdr, params={"state": "open"}
        ).json()
        assert "v" in {i["title"] for i in listing}


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


def test_byo_roster_person_may_hold_many_groups(tmp_path):
    """A person is rarely exactly one group: squads, compliance registers and region-scoped
    grants sit on top of the department. An entry's `groups` list states those memberships;
    the department membership stays, so a squad-less roster is unchanged by the feature."""
    roster = tmp_path / "roster.yaml"
    roster.write_text(
        yaml.safe_dump(
            {
                "org": "redwood",
                "org_domain": "redwoodinference.com",
                "departments": {
                    "Engineering": [
                        {"name": "Ava Chen", "email": "ava.chen@redwoodinference.com"},
                        {
                            "name": "Bo Ryu",
                            "email": "bo.ryu@redwoodinference.com",
                            # a repeat of the department and an unslugged name, both normalized
                            "groups": ["proj-checkout-rework", "Engineering", "res-emea-support"],
                        },
                    ]
                },
                "contacts": [
                    {
                        "name": "Zoe Newperson",
                        "email": "zoe.newperson@redwoodinference.com",
                        "group": "engineering",
                        "groups": ["comp-hr-investigations"],
                    }
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
                "group": "proj-checkout-rework",
                "visibility": "group",
                "title": "t",
                "content": "c",
                "author_email": "ava.chen@redwoodinference.com",
            }
        ],
    )
    settings = Settings(data_dir=tmp_path)
    load(corpus, settings, roster=roster)
    conn = store.connect_ro(settings.db_path)
    try:
        members = {
            (r["group_id"], r["user_id"]) for r in conn.execute("SELECT * FROM group_members")
        }
        assert members == {
            ("engineering", "ava.chen@redwoodinference.com"),
            ("engineering", "bo.ryu@redwoodinference.com"),
            ("proj-checkout-rework", "bo.ryu@redwoodinference.com"),
            ("res-emea-support", "bo.ryu@redwoodinference.com"),
            ("engineering", "zoe.newperson@redwoodinference.com"),
            ("comp-hr-investigations", "zoe.newperson@redwoodinference.com"),
        }
        assert {r["id"] for r in conn.execute("SELECT id FROM principals WHERE type='group'")} == {
            "engineering",
            "proj-checkout-rework",
            "res-emea-support",
            "comp-hr-investigations",
        }
        # the extra memberships change who a group-scoped clause admits, nothing about tokens
        tokens = yaml.safe_load(settings.tokens_path.read_text())
        assert {u["email"] for u in tokens["users"]} == {
            "ava.chen@redwoodinference.com",
            "bo.ryu@redwoodinference.com",
        }
    finally:
        conn.close()


def test_byo_roster_duplicate_entries_union_their_groups(tmp_path):
    """A person listed under two departments — or as a contact carrying an extra register
    on top of their department entry — holds the UNION of the memberships. Replacing the
    entry dropped the earlier groups, and a group-scoped document then wrongly denied the
    person. A scalar `groups:` is one group, not a character sequence."""
    from backlot.importer.byo import load_roster

    roster = tmp_path / "roster.yaml"
    roster.write_text(
        yaml.safe_dump(
            {
                "org": "redwood",
                "org_domain": "redwoodinference.com",
                "departments": {
                    "Engineering": [{"name": "Bo Ryu", "email": "bo@redwoodinference.com"}],
                    "Security": [
                        {
                            "name": "Bo Ryu",
                            "email": "bo@redwoodinference.com",
                            "groups": "res-emea-support",  # scalar: one group
                        }
                    ],
                },
                "contacts": [
                    {
                        "name": "Bo Ryu",
                        "email": "bo@redwoodinference.com",
                        "groups": ["comp-hr-investigations", 2024],
                    }
                ],
            }
        )
    )
    parsed = load_roster(roster)
    bo = parsed["users"]["bo@redwoodinference.com"]
    assert bo["token"] is True  # the contact entry never demoted the account
    # A set, not a list: downstream this becomes group membership, and the rendered order
    # rides on `yaml.safe_dump`'s key sorting rather than on anything this code decides.
    assert set(bo["groups"]) == {
        "engineering",
        "security",
        "res-emea-support",
        "comp-hr-investigations",
        "2024",
    }
    assert len(bo["groups"]) == 5  # deduplicated, so no membership row is doubled


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
                complete(
                    **{"source_type": "confluence", "doc_id": "c1", "title": "t", "content": body}
                ),
                complete(
                    **{
                        "source_type": "confluence",
                        "doc_id": "c2",
                        "title": "t2",
                        "content": "second",
                    }
                ),
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
        assert (
            store.get_document(conn, "confluence", served_id("confluence", "c1"))["content"] == body
        )
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
        rows = store.gmail_thread(conn, served_id("gmail", "gm-deck"))
        assert [(r["id"], r["thread_seq"]) for r in rows] == [
            (served_id("gmail", "gm-1"), 0),
            (served_id("gmail", "gm-1::m1"), 1),
        ]
        assert {r["thread_id"] for r in rows} == {served_id("gmail", "gm-deck")}
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
            complete(
                **{
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
                        {
                            "content": "third, dated later still",
                            "created_ts": "2026-02-11T08:00:00Z",
                        },
                        {"content": "fourth, undated"},
                    ],
                }
            ),
        ],
    )
    settings = Settings(data_dir=tmp_path)
    load(corpus, settings)
    conn = store.connect_ro(settings.db_path)
    try:
        rows = store.doc_comments(conn, "linear", served_id("linear", "ln-1"))
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
        key = conn.execute("SELECT key FROM jira_issues").fetchone()[0]
        assert [r["created_ts"] for r in store.doc_comments(conn, "jira", key)] == [
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
        assert store.doc_grants(conn, "gmail", served_id("gmail", "gm-dark")) == []
        # absent readers -> the org default
        assert store.doc_grants(conn, "gmail", served_id("gmail", "gm-open"))
        # ...so it is invisible to a user token and reachable only by admin (visible_ids=None)
        assert (
            store.get_document(conn, "gmail", served_id("gmail", "gm-dark"), visible_ids={"acme"})
            is None
        )
        assert store.get_document(conn, "gmail", served_id("gmail", "gm-dark")) is not None
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
            fh.write(_js.dumps(complete(**rec)) + "\n")
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
    one to fewer records leaves a well-formed gzip stream that nothing downstream notices, so
    without this check the import reports success on a corpus missing documents the manifest
    counts."""
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
        byo.run(out)
    assert e.value.code == 1
    assert not (data / "mock.sqlite").exists(), "a rejected artifact must not leave a database"


def test_a_single_gzipped_corpus_file_loads(tmp_path):
    """The README documents `backlot import corpus.jsonl.gz`; every other test reaches a
    `.gz` through a shard directory, so the plain single-file case had no coverage."""
    corpus = tmp_path / "corpus.jsonl.gz"
    with io.TextIOWrapper(gzip.GzipFile(corpus, "wb", mtime=0), encoding="utf-8") as fh:
        fh.write(
            json.dumps(
                complete(
                    **{
                        "source_type": "slack",
                        "channel": "general",
                        "author_email": "ava@acme.com",
                        "content": "Gzipped.",
                    }
                )
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
                complete(
                    **{
                        "source_type": "confluence",
                        "space": "handbook",
                        "doc_id": "shared-1",
                        "title": "Sprint plan (page)",
                        "content": "The confluence rendering.",
                        "author_email": "ava@acme.com",
                    }
                ),
                complete(
                    **{
                        "source_type": "google_drive",
                        "folder": "users",
                        "doc_id": "shared-1",
                        "title": "Sprint plan (doc)",
                        "content": "The drive document.",
                        "author_email": "ava@acme.com",
                    }
                ),
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
        conn.execute(
            "SELECT title FROM confluence_pages WHERE id = ?",
            (served_id("confluence", "shared-1"),),
        ).fetchone()[0]
        == "Sprint plan (page)"
    )
    assert (
        conn.execute(
            "SELECT title FROM gdrive_files WHERE id = ?", (served_id("google_drive", "shared-1"),)
        ).fetchone()[0]
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
            complete(
                **{
                    "source_type": "confluence",
                    "space": "h",
                    "title": "A",
                    "content": "a",
                    "author_email": "ava@acme.com",
                }
            )
        )
    )
    second = tmp_path / "b.jsonl"
    second.write_text(
        json.dumps(
            complete(
                **{
                    "source_type": "confluence",
                    "space": "h",
                    # An append into a probed source states the id it wants: without
                    # one this row could not be told apart from a re-import of the first.
                    "content_id": 4242,
                    "title": "B",
                    "content": "b",
                    "author_email": "ava@acme.com",
                }
            )
        )
    )
    load(first, settings)
    load(second, settings, reset=False)
    conn = store.connect_ro(settings.db_path)
    assert store.read_meta(conn, "source_documents") == "2"
    conn.close()


def test_hello_corpus_loads_and_covers_every_source(tmp_path):
    """The wheel's built-in corpus must load and exercise EVERY served source.

    Driven off `store.SOURCE_TABLE` rather than a list written out here, so adding a twelfth source
    fails this test until the bundled corpus covers it too — which is how fireflies came to be
    missing from the corpus while every other source had rows.
    """
    hello = Path(__file__).resolve().parent.parent / "backlot" / "data" / "hello.jsonl"
    settings = Settings(data_dir=tmp_path)
    load(hello, settings)
    conn = store.connect_ro(settings.db_path)
    counts = {
        src: conn.execute(f"SELECT COUNT(*) FROM {store.table(src)}").fetchone()[0]
        for src in store.SOURCE_TABLE
    }
    for src, n in counts.items():
        assert n > 0, f"hello corpus has no {src} rows"
    # The two counts must differ, or the corpus does not demonstrate the parsing layer.
    assert int(store.read_meta(conn, "source_documents")) < sum(counts.values())
    # Every child-row table is exercised too: a comment API with nothing behind it teaches a
    # reader that the mock has no comments rather than that this corpus has none.
    for src, tbl in store.COMMENT_TABLE.items():
        n = conn.execute(f"SELECT COUNT(*) FROM {tbl}").fetchone()[0]
        assert n > 0, f"hello corpus has no {src} child rows ({tbl})"
    # More than one container per source, so a listing endpoint has something to page.
    for src in store.SOURCE_TABLE:
        gtable, gcol = store.GROUPING[src]
        n = conn.execute(f"SELECT COUNT(*) FROM {gtable}").fetchone()[0]
        assert n >= 2, f"hello corpus has only {n} {gcol}(s) for {src}"
    conn.close()


def test_byo_roster_a_stated_name_beats_a_derived_one(tmp_path):
    """Memberships union, but names do not, so first-seen-wins is wrong for them: `name`
    always has a fallback derived from the address and therefore never looks absent. An
    entry stating "Tomás Rré" lost to an earlier entry that stated nothing, and the corpus
    served "Tomas Rre" — a name the address cannot round-trip back to."""
    from backlot.importer.byo import load_roster

    email = "tomas.rre@redwoodinference.com"

    def roster(name, departments):
        p = tmp_path / f"{name}.yaml"
        p.write_text(
            yaml.safe_dump({"departments": departments}, allow_unicode=True), encoding="utf-8"
        )
        return load_roster(p)["users"][email]

    # Derived first, stated second — the order in which the accent is easiest to lose.
    assert (
        roster(
            "a",
            {
                "Engineering": [{"email": email}],
                "Security": [{"name": "Tomás Rré", "email": email}],
            },
        )["name"]
        == "Tomás Rré"
    )
    # Stated first, derived second — unchanged.
    assert (
        roster(
            "b",
            {
                "Aaa": [{"name": "Tomás Rré", "email": email}],
                "Bbb": [{"email": email}],
            },
        )["name"]
        == "Tomás Rré"
    )
    # Two stated names: the first still wins, as memberships do.
    assert (
        roster(
            "c",
            {
                "Aaa": [{"name": "First Stated", "email": email}],
                "Bbb": [{"name": "Second Stated", "email": email}],
            },
        )["name"]
        == "First Stated"
    )


def test_byo_roster_a_list_under_the_singular_group_key_is_read_not_crashed(tmp_path):
    """`groups:` accepts a scalar because the sibling field is one; adding the plural makes
    the mirror slip likelier. A list under `group:` reached `slugify` and raised
    `AttributeError: 'list' object has no attribute 'lower'`, naming neither the file nor
    the key. Every entry is kept — trading the crash for a silent loss would be no better."""
    from backlot.importer.byo import load_roster

    roster = tmp_path / "roster.yaml"
    roster.write_text(
        yaml.safe_dump(
            {
                "contacts": [
                    {"name": "A", "email": "a@x.com", "group": ["engineering", "security"]},
                    {"name": "B", "email": "b@x.com", "group": "engineering"},
                ]
            }
        )
    )
    users = load_roster(roster)["users"]
    assert set(users["a@x.com"]["groups"]) == {"engineering", "security"}
    assert users["b@x.com"]["groups"] == ["engineering"]  # the scalar path is unchanged


def test_byo_roster_group_and_groups_read_the_same_in_every_shape(tmp_path):
    """Neither field's meaning may depend on how it is written. A department entry's own
    `group:` was read only as a list, so the scalar — the likelier spelling — vanished
    without a word; and a `groups:` scalar was read only as a string, so a bare `2024`
    raised while `[2024]` slugified. One reader for both fields makes the shapes uniform."""
    from backlot.importer.byo import load_roster

    roster = tmp_path / "roster.yaml"
    roster.write_text(
        yaml.safe_dump(
            {
                "departments": {
                    "Engineering": [
                        {"email": "a@x.com", "group": "squad-checkout"},
                        {"email": "b@x.com", "group": ["squad-checkout", "squad-ledger"]},
                        {"email": "c@x.com", "groups": 2024},
                        {"email": "d@x.com", "groups": [2024]},
                        {"email": "e@x.com", "group": "squad-checkout", "groups": "squad-checkout"},
                    ]
                }
            }
        )
    )
    users = load_roster(roster)["users"]
    # A department entry's `group:` is an extra membership, whichever shape states it.
    assert users["a@x.com"]["groups"] == ["engineering", "squad-checkout"]
    assert users["b@x.com"]["groups"] == ["engineering", "squad-checkout", "squad-ledger"]
    # A bare number is one group named "2024", not a TypeError and not its digits.
    assert users["c@x.com"]["groups"] == ["engineering", "2024"]
    assert users["d@x.com"]["groups"] == users["c@x.com"]["groups"]
    # Naming one group across both fields still yields one row.
    assert users["e@x.com"]["groups"] == ["engineering", "squad-checkout"]


# --- every probe's walk is BOUNDED, and gives up loudly -----------------------------------
# Five probes assign an id by walking from a seed until one is free, each within its own range.
# All five must RAISE on an exhausted range rather than return a value they already know is taken,
# which would land two documents on one id.
#
# Real exhaustion needs ~9,000 to ~90,000 rows in one container, so each case shrinks its own range
# constant to 2 and collapses its seed to a constant. Which patch reaches which differs: a RANGE is
# a plain int read at call time, so `setattr` works; a seed the `store.ID_SEED` registry captured at
# import needs `setitem`, and a seed the assigner reads off `synth` at call time needs `setattr`.
#
# The collapsed seed MUST land inside the shrunk range. A seed outside it spends the walk's first
# step moving into range rather than checking a candidate, so with range 2 only one of the two slots
# is ever checked and the raise leaves the other free — a premature give-up that `pytest.raises`
# cannot tell from a real one.
#
# 4 rows, not 3: with a constant seed the first candidate is always the already-taken seed, so a
# 2-value range gives one real chance per row. 3 rows leaves a candidate never probed, which a
# mutation returning the walk's last unchecked value slips through.

_EXHAUSTION_CASES = [
    pytest.param(
        "GITHUB_NUMBER_RANGE",
        lambda mp, synth: mp.setitem(store.ID_SEED, "github", (lambda seed: 999999, "repo")),
        lambda i: {
            "source_type": "github",
            "doc_id": f"g{i}",
            "repo": "core",
            "title": f"Issue {i}",
            "content": "x",
            "author_email": "a@acme.com",
        },
        id="github-number",
    ),
    pytest.param(
        "JIRA_KEY_NUMBER_RANGE",
        lambda mp, synth: mp.setattr(synth, "jira_key_number", lambda doc_id: 1),
        lambda i: {
            "source_type": "jira",
            "doc_id": f"j{i}",
            "project": "payments",
            "title": f"Issue {i}",
            "content": "x",
            "author_email": "a@acme.com",
        },
        id="jira-key-suffix",
    ),
    pytest.param(
        "CONFLUENCE_ID_RANGE",
        lambda mp, synth: mp.setitem(
            store.ID_SEED, "confluence", (lambda seed: synth.CONFLUENCE_ID_MIN, None)
        ),
        lambda i: {
            "source_type": "confluence",
            "doc_id": f"c{i}",
            "space": "eng",
            "title": f"Page {i}",
            "content": "x",
            "author_email": "a@acme.com",
        },
        id="confluence-content_id",
    ),
    pytest.param(
        "HUBSPOT_ID_RANGE",
        lambda mp, synth: mp.setitem(
            store.ID_SEED, "hubspot", (lambda seed: synth.HUBSPOT_ID_MIN, None)
        ),
        lambda i: {
            "source_type": "hubspot",
            "doc_id": f"hs{i}",
            "object_type": "contacts",
            "title": f"Contact {i}",
            "content": "x",
            "author_email": "a@acme.com",
        },
        id="hubspot-record_id",
    ),
]


@pytest.mark.parametrize("range_name, patch_seed, record", _EXHAUSTION_CASES)
def test_byo_an_exhausted_id_range_fails_loudly(
    tmp_path, monkeypatch, range_name, patch_seed, record
):
    from backlot import synth
    from tests._helpers import build_corpus

    monkeypatch.setattr(synth, range_name, 2)
    patch_seed(monkeypatch, synth)
    with pytest.raises(SystemExit, match="exhausted"):
        build_corpus(tmp_path, [record(i) for i in range(4)])


def test_byo_an_exhausted_comment_id_range_fails_loudly(tmp_path, monkeypatch):
    """The same bound on a CHILD id, whose four candidates hang off one parent rather than being
    four documents."""
    from backlot import synth
    from tests._helpers import build_corpus

    monkeypatch.setattr(synth, "GITHUB_COMMENT_ID_RANGE", 2)
    monkeypatch.setattr(synth, "github_comment_id", lambda cid: synth.GITHUB_COMMENT_ID_MIN)
    with pytest.raises(SystemExit, match="exhausted"):
        build_corpus(
            tmp_path,
            [
                {
                    "source_type": "github",
                    "doc_id": "g0",
                    "repo": "core",
                    "title": "Issue 0",
                    "content": "x",
                    "author_email": "a@acme.com",
                    "comments": [
                        {"content": f"c{i}", "author_email": "a@acme.com"} for i in range(4)
                    ],
                }
            ],
        )


def test_byo_two_records_cannot_claim_one_tracker_id(tmp_path):
    """Two records claiming the same github number, or the same jira key, must be refused. Accepted,
    one of them is unreachable at the only id it advertises. The loader is the one place that sees
    every row, so it is the only place the claim can be checked — and a corpus stating a fact twice
    is the corpus's mistake to hear about, not a silent loss."""
    import pytest

    from tests._helpers import build_corpus

    def rows(*extra):
        return list(extra)

    with pytest.raises(SystemExit) as e:
        build_corpus(
            tmp_path / "gh",
            rows(
                {
                    "source_type": "github",
                    "doc_id": "g-a",
                    "repo": "core",
                    "subtype": "issue",
                    "title": "A",
                    "content": "one",
                    "author_email": "ava@acme.com",
                    "number": 500,
                },
                {
                    "source_type": "github",
                    "doc_id": "g-b",
                    "repo": "core",
                    "subtype": "issue",
                    "title": "B",
                    "content": "two",
                    "author_email": "ava@acme.com",
                    "number": 500,
                },
            ),
        )
    assert "500" in str(e.value) and "g-a" in str(e.value)

    with pytest.raises(SystemExit) as e:
        build_corpus(
            tmp_path / "jira",
            rows(
                {
                    "source_type": "jira",
                    "doc_id": "j-a",
                    "project": "payments",
                    "title": "JA",
                    "content": "x",
                    "author_email": "ava@acme.com",
                    "key": "PAY-1",
                },
                {
                    "source_type": "jira",
                    "doc_id": "j-b",
                    "project": "payments",
                    "title": "JB",
                    "content": "y",
                    "author_email": "ava@acme.com",
                    "key": "PAY-1",
                },
            ),
        )
    assert "PAY-1" in str(e.value) and "j-a" in str(e.value)

    # The same number in a DIFFERENT repository is a different id, and loads.
    build_corpus(
        tmp_path / "ok",
        rows(
            {
                "source_type": "github",
                "doc_id": "g-c",
                "repo": "core",
                "subtype": "issue",
                "title": "C",
                "content": "one",
                "author_email": "ava@acme.com",
                "number": 500,
            },
            {
                "source_type": "github",
                "doc_id": "g-d",
                "repo": "gateway",
                "subtype": "issue",
                "title": "D",
                "content": "two",
                "author_email": "ava@acme.com",
                "number": 500,
            },
        ),
    )


def test_byo_a_displaced_jira_key_moves_and_stays_reachable(tmp_path):
    """The jira half of the collision contract: an issue whose derived key was claimed by
    an issue that provided it moves to the next free sequence number in the same project,
    and answers there. Serving and the index read one authority, so the key an issue
    advertises is the key that fetches it.

    Also covers the aliases an issue key must NOT answer at:

    - **Its own synthesized spelling.** One column holds ONE suffix per row, and a stated key
      answering at a second, unrequested one is not something real Jira does either.
    - **Its project's other spellings.** `_jira_container_for_key`'s three-way tolerance (stated
      prefix / synthesized key / literal container name) is right for a JQL project TOKEN, but
      reused for issue-KEY resolution it lets `PAYDF384A-<n>` and `payments-<n>` resolve
      `j-provider` even though its project's SERVED prefix is `PAY` -- two alias NAMESPACES per
      project. Both 404, same as real Jira's `/issue/PAYDF384A-101` would for a project that
      answers at `PAY`.
    - **Any case but the stored one.** That same tolerance matches case-INSENSITIVELY
      (`token.upper()`), which is right for the JQL token; reused for an issue key it lets
      `pay-<n>`/`Pay-<n>` resolve `j-provider` right alongside `PAY-<n>`, even though every stated
      key's prefix is schema-enforced uppercase (`jira.schema.json`'s `key` pattern). Matching the
      stored key directly is exact, and that is asserted below rather than assumed.
    """
    from backlot import synth
    from tests._helpers import build_corpus, client_for

    # 'j-keyless' derives PAY-<n>; the second record provides exactly that key.
    stolen = synth.jira_key("j-keyless", "PAY")
    settings = build_corpus(
        tmp_path,
        [
            {
                "source_type": "jira",
                "doc_id": "j-keyless",
                "project": "payments",
                "title": "derived",
                "content": "one",
                "author_email": "ava@acme.com",
            },
            {
                "source_type": "jira",
                "doc_id": "j-provider",
                "project": "payments",
                "title": "provided",
                "content": "two",
                "author_email": "ava@acme.com",
                "key": stolen,
            },
        ],
    )
    tokens = yaml.safe_load(settings.tokens_path.read_text())
    hdr = {
        "Authorization": "Bearer "
        + next(u["token"] for u in tokens["users"] if u["email"] == "ava@acme.com")
    }
    with client_for(settings, reload=True) as c:
        found = c.get(
            "/atlassian/rest/api/3/search/jql", headers=hdr, params={"jql": "project = PAY"}
        ).json()
        served = {i["fields"]["summary"]: i["key"] for i in found["issues"]}
        assert served["provided"] == stolen
        assert served["derived"] != stolen
        for summary, key in served.items():
            got = c.get(f"/atlassian/rest/api/3/issue/{key}", headers=hdr)
            assert got.status_code == 200, (summary, key)
            assert got.json()["fields"]["summary"] == summary

        # Pass 3's dropped alias: j-provider's OWN synthesized spelling under its project's
        # served prefix ("PAY") is a DIFFERENT key from `stolen` (a different doc_id's hash), and
        # must 404 rather than resolve j-provider a second way.
        own_synth_spelling = synth.jira_key("j-provider", "PAY")
        assert own_synth_spelling != stolen  # sanity: a genuinely distinct spelling to probe
        alias404 = c.get(f"/atlassian/rest/api/3/issue/{own_synth_spelling}", headers=hdr)
        assert alias404.status_code == 404

        # The resolver's OWN alias, over a project that DOES have a provided prefix ("PAY").
        # Neither the synthesized project key nor the literal container name is what this
        # project actually serves, so both must 404 despite `_jira_container_for_key` resolving
        # either one just fine as a JQL project token.
        suffix = stolen.rsplit("-", 1)[1]
        synth_prefix = synth.jira_project_key("payments")
        for alt_key in (f"{synth_prefix}-{suffix}", f"payments-{suffix}"):
            resp = c.get(f"/atlassian/rest/api/3/issue/{alt_key}", headers=hdr)
            assert resp.status_code == 404, alt_key

        # Review round 2: a lowercase or mixed-case spelling of the SAME served key must also
        # 404 -- issue-key resolution is case-sensitive (real Jira's own behaviour, and this
        # project's pre-task-8 one), even though the project TOKEN/picker `_jira_container_
        # for_key` backs is deliberately case-insensitive.
        for cased_key in (stolen.lower(), stolen[0] + stolen[1:].lower()):
            assert cased_key != stolen  # sanity: an actually different spelling
            resp = c.get(f"/atlassian/rest/api/3/issue/{cased_key}", headers=hdr)
            assert resp.status_code == 404, cased_key


def test_byo_a_tracker_id_is_read_from_the_field_its_schema_declares(tmp_path):
    """A tracker id has one spelling, and an unknown one is refused rather than dropped.

    There was a `meta` object that seeded the extras, so `meta: {"number": 3}` was accepted and then
    discarded — the record served under a synthesized number while its own prose cited 3. It went
    unnoticed across a 10k corpus: 2,015 jira keys and 635 github numbers written that way. The
    field is gone, so the same corpus now fails validation naming the key.
    """
    from backlot.validation import record_errors

    for source, extra in (
        ("github", {"repo": "core", "subtype": "issue", "meta": {"number": 777}}),
        ("jira", {"project": "payments", "meta": {"key": "PAY-777"}}),
    ):
        rec = {
            "source_type": source,
            "doc_id": "smug",
            "title": "t",
            "content": "c",
            "author_email": "ava@acme.com",
            **extra,
        }
        assert any("meta" in e for e in record_errors(rec)), f"{source} still accepts meta"

    # and the declared spelling is honoured, which is the whole point of refusing the other one
    from tests._helpers import build_corpus

    settings = build_corpus(
        tmp_path,
        [
            {
                "source_type": "github",
                "doc_id": "plain",
                "repo": "core",
                "subtype": "issue",
                "title": "t",
                "content": "c",
                "author_email": "ava@acme.com",
                "number": 777,
            }
        ],
    )
    conn = sqlite3.connect(settings.db_path)
    conn.row_factory = sqlite3.Row
    assert conn.execute("SELECT number FROM github_items").fetchone()["number"] == 777
    conn.close()


def test_byo_a_repeated_document_may_restate_its_own_tracker_id(tmp_path):
    """Two records sharing a (source, doc_id) are both written — the row-level INSERT OR
    REPLACE leaves the later one, which is what a direct import of the same documents
    produces, and one real corpus has four such pairs, one of them within jira. Such a
    repeat carrying its own provided key was aborting the whole import, naming the very
    doc_id it was inserting. Only a DIFFERENT document violates the claim."""
    from tests._helpers import build_corpus

    rec = {
        "source_type": "jira",
        "doc_id": "jira-x",
        "project": "PAY",
        "title": "t",
        "content": "c",
        "author_email": "ava@acme.com",
        "key": "PAY-7",
    }
    settings = build_corpus(tmp_path, [rec, dict(rec)])
    conn = store.connect_ro(settings.db_path)
    rows = conn.execute(f"SELECT title, key FROM {store.table('jira')}").fetchall()
    assert [(r["title"], r["key"]) for r in rows] == [(rec["title"], "PAY-7")]


def test_byo_a_tracker_id_claim_survives_append(tmp_path):
    """`tracker_ids` lives on a `_Loader` that `load_records` builds fresh per invocation, so
    without seeding it from the DB the whole check ended at the shard boundary: two shards
    appended in separate runs could each provide PAY-7 and neither would be told, leaving the
    loser advertising an id that fetches somebody else. That is the failure this check exists
    to remove, reached through --append."""
    import pytest

    from backlot.config import Settings
    from backlot.importer.byo import load

    def shard(name, doc_id, **extra):
        p = tmp_path / name
        p.write_text(
            json.dumps(
                complete(
                    **{
                        "source_type": "jira",
                        "doc_id": doc_id,
                        "project": "PAY",
                        "title": doc_id,
                        "content": "c",
                        "author_email": "ava@acme.com",
                        "key": "PAY-7",
                        **extra,
                    }
                )
            )
            + "\n"
        )
        return p

    settings = Settings(data_dir=tmp_path)
    first = shard("a.jsonl", "jira-a")
    load(first, settings, reset=True)

    with pytest.raises(SystemExit) as e:
        load(shard("b.jsonl", "jira-b"), settings, reset=False)
    # The claim is refused, and it names the value rather than the earlier row: that row's own
    # corpus identifier did not outlive its import, so there is nothing left to name it by.
    assert "PAY-7" in str(e.value) and "a previous import" in str(e.value)

    # Re-appending a shard already loaded is refused too, and for the same reason: the claim on
    # PAY-7 is in the DB, and the row holding it can no longer be recognised as the one re-stating
    # it. A re-import is unsupported outright, not merely when it collides.
    with pytest.raises(SystemExit, match="a previous import"):
        load(first, settings, reset=False)

    # And a number is per repository, so the same one in another repo is not a claim on it.
    gh = tmp_path / "g.jsonl"
    for repo, doc in (("core", "g-a"), ("other", "g-b")):
        gh.write_text(
            json.dumps(
                complete(
                    **{
                        "source_type": "github",
                        "doc_id": doc,
                        "repo": repo,
                        "subtype": "issue",
                        "title": doc,
                        "content": "c",
                        "author_email": "ava@acme.com",
                        "number": 412,
                    }
                )
            )
            + "\n"
        )
        load(gh, settings, reset=False)


def test_byo_a_pull_requests_reviews_link_to_the_number_the_index_resolved(tmp_path):
    """Every other handler reads `_issue_number`; this one derived. When a PR provides no
    number and the derived one is already held by a row that did provide it, the index moves
    this row — and the reviews body went on citing the derived number, so it linked to the
    displacing PR, a different document, from a response about this one."""
    from backlot import synth
    from tests._helpers import build_corpus, client_for

    stolen = synth.github_number("g-victim")
    settings = build_corpus(
        tmp_path,
        [
            {
                "source_type": "github",
                "doc_id": "g-victim",
                "repo": "core",
                "subtype": "pull_request",
                "title": "victim",
                "content": "v",
                "author_email": "ava@acme.com",
                "reviews": [{"author_email": "bob@acme.com", "state": "APPROVED"}],
            },
            {
                "source_type": "github",
                "doc_id": "g-a-thief",
                "repo": "core",
                "subtype": "pull_request",
                "title": "thief",
                "content": "t",
                "author_email": "ava@acme.com",
                "number": stolen,
            },
        ],
    )
    tokens = yaml.safe_load(settings.tokens_path.read_text())
    hdr = {
        "Authorization": "Bearer "
        + next(u["token"] for u in tokens["users"] if u["email"] == "ava@acme.com")
    }
    with client_for(settings, reload=True) as c:
        listing = c.get(
            "/github/repos/acme/core/issues", headers=hdr, params={"state": "open"}
        ).json()
        displaced = next(i["number"] for i in listing if i["title"] == "victim")
        assert displaced != stolen

        reviews = c.get(f"/github/repos/acme/core/pulls/{displaced}/reviews", headers=hdr).json()
        assert reviews, "the victim's own reviews"
        for rv in reviews:
            assert rv["pull_request_url"].endswith(f"/pulls/{displaced}")
            assert f"/pull/{displaced}#" in rv["html_url"]


def test_byo_two_projects_cannot_share_a_provided_key_prefix(tmp_path):
    """A key's prefix is its project's key, and real Jira holds that unique across projects.
    The index can only give `PAY` to one container, so a second project providing `PAY-`
    keys loaded fine and then `project = PAY` JQL, the picker and the role endpoint all
    silently served only the first — the same one-holder rule as a full key, one level up.
    The claim must also hold across `--append`, where the earlier keys are only in the DB."""
    from tests._helpers import build_corpus

    def rec(did, project, key):
        return complete(
            source_type="jira",
            doc_id=did,
            project=project,
            title=did,
            content="c",
            author_email="ava@acme.com",
            key=key,
        )

    # Two keys under one prefix in ONE project is the normal case and loads.
    with pytest.raises(SystemExit) as e:
        build_corpus(
            tmp_path / "one",
            [
                rec("j-a", "payments", "PAY-1"),
                rec("j-b", "payments", "PAY-3"),
                rec("j-c", "billing", "PAY-2"),
            ],
        )
    assert "PAY" in str(e.value) and "payments" in str(e.value)

    # And across runs: the first shard's claim is seeded from the DB, not remembered.
    settings = Settings(data_dir=tmp_path)
    first = tmp_path / "a.jsonl"
    first.write_text(json.dumps(rec("j-a", "payments", "PAY-1")))
    load(first, settings, reset=True)
    second = tmp_path / "b.jsonl"
    second.write_text(json.dumps(rec("j-b", "billing", "PAY-2")))
    with pytest.raises(SystemExit) as e:
        load(second, settings, reset=False)
    assert "PAY" in str(e.value) and "payments" in str(e.value)
    # Re-appending the holder itself is refused too: its key is claimed in the DB and the row
    # holding it can no longer be recognised as the one re-stating it.
    with pytest.raises(SystemExit, match="a previous import"):
        load(first, settings, reset=False)


def _jira_rec(did, project, key=None):
    r = {
        "source_type": "jira",
        "doc_id": did,
        "project": project,
        "title": did,
        "content": "c",
        "author_email": "ava@acme.com",
        "created": "2026-02-01T00:00:00Z",
    }
    if key:
        r["key"] = key
    return r


@pytest.mark.parametrize(
    "project",
    ["platform-infra-reliability-and-cost-ops", "3d-printing"],
    ids=["six-word-name", "name-starting-with-a-digit"],
)
def test_byo_a_project_can_state_the_key_it_was_served(tmp_path, project):
    """An --append MUST state a key (`_require_provided_id`), so the key the mock serves has to be
    one the corpus is allowed to write back. Two shapes of project name had none: omitting the key
    is refused by the importer, and the key they were served was refused by validation.

    Both shapes are facts about the NAME, which is what `jira_project_key` derives from. A name past
    four words used to carry the key past real Jira's ten characters, and a name whose first word
    starts with a digit used to hand the key that digit. The cap ends both, so what this asserts is
    that the round trip closes: import, read the served project key, append an issue stating it."""
    settings = Settings(data_dir=tmp_path / "d")
    load(_write(tmp_path, [_jira_rec("j-1", project)], "a.jsonl"), settings)
    conn = store.connect_ro(settings.db_path)
    project_key = conn.execute("SELECT key FROM jira_projects").fetchone()["key"]
    assert project_key == synth.jira_project_key(project)
    # The append states the served prefix, which is the only key that keeps the project on one
    # prefix -- anything else is refused by the 1:1 prefix claim instead.
    load(
        _write(tmp_path, [_jira_rec("j-2", project, f"{project_key}-2")], "b.jsonl"),
        settings,
        reset=False,
    )
    conn = store.connect_ro(settings.db_path)
    assert sorted(r["key"] for r in conn.execute("SELECT key FROM jira_issues")) == sorted(
        [synth.jira_key("j-1", project_key), f"{project_key}-2"]
    )


@pytest.mark.parametrize("bad", ["pay-1", "PAY 1", "1", "PAY", "PAY-0", "PAY-1-x", "A-1"])
def test_byo_a_mistyped_jira_key_is_refused(tmp_path, bad):
    """The prefix is a fact about the whole project, so a typo in one key renames every issue in it.
    Capping the derivation is what makes the served key legal to state, and the rule a corpus is
    held to does not move with it: this pattern is the one main already enforced.

    `A-1` is in the list on purpose: real Jira rejects a single-character project key and so do
    strict clients (see `synth._key`), and no derivation produces one -- `jira_project_key` is at
    least seven characters. `PAY-1-x` matters for a different reason: the loader reads a prefix by
    splitting on the LAST hyphen, so admitting a second one would let `PAY-1` become the project's
    key."""
    with pytest.raises(SystemExit, match=r"\[key\]"):
        load(
            _write(tmp_path, [_jira_rec("j-1", "payments", bad), _jira_rec("j-2", "payments")]),
            Settings(data_dir=tmp_path / "d"),
        )


def _linear_rec(did, team="payments-platform", identifier=None):
    r = complete(
        "linear",
        doc_id=did,
        team=team,
        title=did,
        content="c",
        author_email="ava@acme.com",
        created="2026-02-01T00:00:00Z",
    )
    if identifier:
        r["identifier"] = identifier
    return r


def _linear_shard(tmp_path, name, recs):
    p = tmp_path / name
    p.write_text("".join(json.dumps(complete(**r)) + "\n" for r in recs))
    return p


def _stored_identifiers(settings, dids):
    """dataset id -> the identifier its row landed on.

    Read back through `served_id`, because a corpus's own identifier does not outlive the import: a
    linear row's served `id` is a pure function of it, so the mapping is recomputable even though
    the DB carries no column for it.
    """
    conn = store.connect_ro(settings.db_path)
    rows = {
        r["id"]: r["identifier"] for r in conn.execute("SELECT id, identifier FROM linear_issues")
    }
    return {d: rows[served_id("linear", d)] for d in dids}


def test_byo_linear_provided_prefix_teaches_the_team(tmp_path):
    """A provided identifier's prefix is a fact about its TEAM: real Linear derives an identifier
    from its team's key, so one team never serves two spellings. A keyless sibling loaded after the
    provided one materializes with the claimed prefix; one loaded BEFORE it is re-stamped at load
    end, where the whole container is visible; and one whose re-stamped number would collide with a
    provided identifier probes forward to the next free number."""
    # keyless BEFORE the provided one (re-stamped), keyless AFTER it (in flow), and a provided
    # identifier squatting exactly on the number the re-stamp would otherwise take.
    before_n = synth.linear_issue_number(synth.linear_identifier("ln-before", "ENG"))
    settings = Settings(data_dir=tmp_path / "one")
    load(
        _linear_shard(
            tmp_path,
            "a.jsonl",
            [
                _linear_rec("ln-before"),
                _linear_rec("ln-a", identifier="ENG-7"),
                _linear_rec("ln-squat", identifier=f"ENG-{before_n}"),
                _linear_rec("ln-after"),
            ],
        ),
        settings,
    )
    dids = ["ln-before", "ln-a", "ln-squat", "ln-after"]
    idents = _stored_identifiers(settings, dids)
    assert all(v.startswith("ENG-") for v in idents.values()), idents
    assert idents["ln-after"] == synth.linear_identifier("ln-after", "ENG")
    # the squatted number forced the re-stamp one step forward
    assert idents["ln-before"] == f"ENG-{before_n % synth.LINEAR_ISSUE_NUMBER_RANGE + 1}"
    assert len(set(idents.values())) == 4
    # and the team is served under the key its issues spell out, not the one its name derives
    conn = store.connect_ro(settings.db_path)
    assert store.linear_team_keys(conn) == {"payments-platform": "ENG"}
    assert synth.linear_team_key("payments-platform") == "PP"  # the derivation it overrode

    # across --append: the stored `served_key` is what the next shard materializes under, so a
    # keyless row arriving in a later shard carries the claimed prefix with no re-stamp at all.
    s2 = Settings(data_dir=tmp_path / "two")
    load(_linear_shard(tmp_path, "b1.jsonl", [_linear_rec("ln-a", identifier="ENG-7")]), s2)
    load(_linear_shard(tmp_path, "b2.jsonl", [_linear_rec("ln-c")]), s2, reset=False)
    appended = _stored_identifiers(s2, ["ln-a", "ln-c"])
    assert appended["ln-a"] == "ENG-7"
    assert appended["ln-c"] == synth.linear_identifier("ln-c", "ENG")


def test_byo_linear_prefixes_hold_one_to_one(tmp_path):
    """A team has one key and a key names one team — real Linear keeps keys workspace-unique, and
    the identifier scheme depends on it. Both directions are refused at load with the holder
    named, this run or seeded across --append."""
    with pytest.raises(SystemExit, match="which team 'payments-platform' already holds"):
        load(
            _linear_shard(
                tmp_path,
                "a.jsonl",
                [
                    _linear_rec("ln-a", identifier="ENG-7"),
                    _linear_rec("ln-b", team="growth", identifier="ENG-9"),
                ],
            ),
            Settings(data_dir=tmp_path / "one"),
        )
    with pytest.raises(SystemExit, match="already name it 'ENG'"):
        load(
            _linear_shard(
                tmp_path,
                "b.jsonl",
                [_linear_rec("ln-a", identifier="ENG-7"), _linear_rec("ln-b", identifier="PAY-9")],
            ),
            Settings(data_dir=tmp_path / "two"),
        )
    # across --append: the earlier claim is seeded from the team's stored `served_key`
    s = Settings(data_dir=tmp_path / "three")
    load(_linear_shard(tmp_path, "c1.jsonl", [_linear_rec("ln-a", identifier="ENG-7")]), s)
    with pytest.raises(SystemExit, match="which team 'payments-platform' already holds"):
        load(
            _linear_shard(
                tmp_path, "c2.jsonl", [_linear_rec("ln-b", team="growth", identifier="ENG-9")]
            ),
            s,
            reset=False,
        )


def test_byo_a_teams_key_is_settled_by_its_first_import(tmp_path):
    """An --append cannot rename a team. Every identifier already stored is prefixed with the key
    that import settled on, and re-stamping them is not on the table: they are the ids clients hold,
    and a stored identifier cannot even say whether its prefix was the corpus's or this mock's --
    both spellings share the one column. So the shard that would rename the team is refused
    instead, naming the key its issues already carry."""
    settings = Settings(data_dir=tmp_path / "d")
    load(_linear_shard(tmp_path, "s1.jsonl", [_linear_rec("ln-keyless")]), settings)
    conn = store.connect_ro(settings.db_path)
    assert store.linear_team_keys(conn) == {"payments-platform": "PP"}
    with pytest.raises(SystemExit, match="already name it 'PP'"):
        load(
            _linear_shard(tmp_path, "s2.jsonl", [_linear_rec("ln-states-it", identifier="ENG-7")]),
            settings,
            reset=False,
        )
    # A shard that agrees with the settled key is fine, and claims its own spelling.
    load(
        _linear_shard(tmp_path, "s3.jsonl", [_linear_rec("ln-states-it", identifier="PP-7")]),
        settings,
        reset=False,
    )
    assert _stored_identifiers(settings, ["ln-states-it"])["ln-states-it"] == "PP-7"


def test_byo_linear_identifiers_do_not_depend_on_where_the_provided_line_sits(tmp_path):
    """The identifier a keyless row ends up with is a function of the container, not of the file.
    Stamping in the record loop can only use the prefix known SO FAR, so a row before the provided
    line and a row after it were settled by different rules: the same corpus, with that one line
    moved, produced a different identifier for every keyless row and a different number of distinct
    ones. Both orders are loaded here and compared row by row."""
    # Enough rows that the derived numbers collide (600 in a 9,000 space collides ~20 times),
    # which is what makes the two orders settle differently at all.
    keyless = [_linear_rec(f"ln-{i:04d}") for i in range(600)]
    provided = _linear_rec("ln-states-it", identifier="ENG-7")
    dids = [r["doc_id"] for r in (*keyless, provided)]
    out = []
    for name, recs in (("first", [provided, *keyless]), ("last", [*keyless, provided])):
        settings = Settings(data_dir=tmp_path / name)
        load(_linear_shard(tmp_path, f"{name}.jsonl", recs), settings)
        out.append(_stored_identifiers(settings, dids))
    assert out[0] == out[1], "the provided line's position changed the identifiers"
    assert all(v.startswith("ENG-") for v in out[0].values())
    assert len(set(out[0].values())) == len(out[0]), "a container's identifiers collided"


def test_byo_a_restamp_never_lands_on_another_teams_identifier(tmp_path):
    """The prefix a team states can be the key another team derives from its NAME, and the 1:1
    refusal deliberately does not cover that pairing — the derived one was never written down, so
    two containers reducing to one key stays as legal as it has always been. Re-stamping into the
    shared prefix then put two teams' rows on one spelling, and `issue(id:)` can only answer with
    one: the other document answers at an id that fetches its neighbour. So the free-number search
    is bucketed by PREFIX, which spans the workspace, and not by container."""
    assert synth.linear_team_key("engineering") == "ENG"  # the pairing this test needs
    # a dataset id in the stating team whose ENG-materialisation is some engineering row's too
    wanted = synth.linear_identifier("ln-collide", "ENG")
    twin = next(
        c
        for c in (f"pp-{i}" for i in range(200_000))
        if synth.linear_identifier(c, "ENG") == wanted
    )
    settings = Settings(data_dir=tmp_path / "d")
    load(
        _linear_shard(
            tmp_path,
            "c.jsonl",
            [
                _linear_rec("ln-collide", team="engineering"),
                _linear_rec("ln-states-it", identifier="ENG-7"),
                _linear_rec(twin),
            ],
        ),
        settings,
    )
    idents = _stored_identifiers(settings, ["ln-collide", "ln-states-it", twin])
    assert idents["ln-collide"] == wanted, "the engineering row lost its derived spelling"
    assert idents[twin] != wanted, "the re-stamp landed on another team's identifier"
    assert len(set(idents.values())) == 3


def test_byo_a_materialized_identifier_yields_to_a_corpus_that_states_it(tmp_path):
    """A materialized identifier is a hash, not a claim. A keyless row is stamped the moment its
    record lands, so it can take a spelling a LATER record states outright — and the probe that
    placed it had no way to know. The stated id wins and the stamped row steps aside at the end of
    the same load, because the corpus's own issue is the one whose documents cite that id."""
    # The value `ln-keyless` materializes under its own team's key, then stated by another record.
    stated = synth.linear_identifier("ln-keyless", "PP")
    settings = Settings(data_dir=tmp_path / "d")
    load(
        _linear_shard(
            tmp_path,
            "c.jsonl",
            [_linear_rec("ln-keyless"), _linear_rec("ln-states-it", identifier=stated)],
        ),
        settings,
    )
    idents = _stored_identifiers(settings, ["ln-keyless", "ln-states-it"])
    assert idents["ln-states-it"] == stated
    assert idents["ln-keyless"] != stated
    assert idents["ln-keyless"].startswith("PP-")


def test_byo_appending_to_a_settled_team_leaves_every_identifier_alone(tmp_path):
    """Re-stamping runs on every load, so it has to be idempotent: an identifier a client may
    already hold must survive both a re-import of the same records and an append of new ones. The
    team's key is read back off `served_key`, so the second load stamps under the prefix the first
    settled on and has nothing to move."""
    settings = Settings(data_dir=tmp_path / "d")
    first = [
        *(_linear_rec(f"ln-{i:04d}") for i in range(200)),
        _linear_rec("ln-p", identifier="ENG-7"),
    ]
    load(_linear_shard(tmp_path, "a.jsonl", first), settings)
    dids = [r["doc_id"] for r in first]
    before = _stored_identifiers(settings, dids)
    assert len(set(before.values())) == len(before)
    load(
        _linear_shard(tmp_path, "again.jsonl", [_linear_rec("ln-p", identifier="ENG-7")]),
        settings,
        reset=False,
    )
    assert _stored_identifiers(settings, dids) == before
    more = [_linear_rec(f"ln-x{i:04d}") for i in range(50)]
    load(_linear_shard(tmp_path, "more.jsonl", more), settings, reset=False)
    after = _stored_identifiers(settings, dids + [r["doc_id"] for r in more])
    assert {d: after[d] for d in before} == before, "an append renumbered an existing row"
    assert len(set(after.values())) == len(after)


@pytest.mark.parametrize("bad", ["eng-7", "ENG 7", "7", "ENG_7", "ENG-", "ENG- 7"])
def test_byo_a_mistyped_linear_identifier_is_refused(tmp_path, bad):
    """The prefix is a fact about the whole team, so a typo in one issue renames every one of them
    — `identifier: "7"` made the team answer at `7` and stamped its siblings `7-n`. That is why
    jira's key carries a pattern, and the same reasoning now reaches Linear's identifier.

    Only the PREFIX is refusable. Length is not a refused shape (see
    test_byo_a_derived_identifier_is_accepted_as_input) and neither is anything after the first
    hyphen (see test_byo_a_slug_shaped_key_claims_only_its_leading_prefix) -- the number half cannot
    rename a team, so validating it would only refuse corpora for no gain."""
    with pytest.raises(SystemExit, match=r"\[identifier\]"):
        load(
            _linear_shard(
                tmp_path, "c.jsonl", [_linear_rec("ln-a", identifier=bad), _linear_rec("ln-b")]
            ),
            Settings(data_dir=tmp_path / "d"),
        )


def test_byo_a_slug_shaped_key_claims_only_its_leading_prefix(tmp_path):
    """`importer.erb`'s linear mapping passes the bench's `key` through verbatim, and 43 of the
    corpus's 35,308 linear documents state a slug rather than an identifier
    (`ENG-453210-kms-hsm-deployment-lifecycle-telemetry-orbiter`). Read to the LAST hyphen, each of
    those renamed `engineering` to a 50-character pseudo-key and stamped the team's keyless issues
    under it, and a sibling stating a real `ENG-…` was then refused for disagreeing with its own
    team. Read to the FIRST, the whole corpus loads unchanged.

    Loaded BOTH ways, and that is half the test. `erb.import_structured` imports with
    `validate=False`, so the schema pattern cannot stand in for the prefix rule on that path -- but
    `erb.export_byo` writes the same slug into `corpus.jsonl`, which `byo.load` reads with
    validation ON. A pattern that refuses the slug turns `backlot export` into an artifact
    `backlot import` cannot read, and breaks the equivalence
    `test_erb_to_byo_round_trip_builds_an_equivalent_database` asserts between the two paths."""
    slug = "ENG-453210-kms-hsm-deployment-lifecycle-telemetry-orbiter"
    recs = [
        _linear_rec("ln-slug", team="engineering", identifier=slug),
        _linear_rec("ln-real", team="engineering", identifier="ENG-7"),
        _linear_rec("ln-keyless", team="engineering"),
    ]
    dids = [r["doc_id"] for r in recs]
    seen = []
    for name, validate in (("validated", True), ("unvalidated", False)):
        settings = Settings(data_dir=tmp_path / name)
        byo.load_records(lambda: enumerate(recs, 1), settings, reset=True, validate=validate)
        conn = store.connect_ro(settings.db_path)
        assert store.linear_team_keys(conn) == {"engineering": "ENG"}
        seen.append(_stored_identifiers(settings, dids))
    assert seen[0] == seen[1], "validation changed what the corpus loaded as"
    idents = seen[0]
    assert idents["ln-slug"] == slug  # the corpus's own spelling, untouched
    assert idents["ln-real"] == "ENG-7"
    assert idents["ln-keyless"] == synth.linear_identifier("ln-keyless", "ENG")


@pytest.mark.parametrize(
    "team",
    ["platform-infra-reliability-and-cost-ops", "3d-printing"],
    ids=["prefix-longer-than-real-linears-5", "prefix-starting-with-a-digit"],
)
def test_byo_a_derived_identifier_is_accepted_as_input(tmp_path, team):
    """Whatever the mock materializes, it must also accept: `synth.linear_team_key` takes one
    initial per word with no upper bound and no leading-letter rule, so it serves `PIRACO-8079` and
    `3P-8079` — and a pattern written to real Linear's 1-5 character team key refused a corpus
    stating the very identifier the mock had handed out. The round trip is the assertion: derive the
    identifier, state it, load with validation on."""
    derived = synth.linear_identifier("ln-a", synth.linear_team_key(team))
    settings = Settings(data_dir=tmp_path / "d")
    load(
        _linear_shard(tmp_path, "c.jsonl", [_linear_rec("ln-a", team=team, identifier=derived)]),
        settings,
    )
    assert _stored_identifiers(settings, ["ln-a"])["ln-a"] == derived


def test_byo_one_project_cannot_provide_two_key_prefixes(tmp_path):
    """`PAY-1` beside `BILL-2` in one project is the other direction of the same 1:1: the
    project can only have one key, so whichever the index picked, the other issue served a
    key whose prefix was not its project's key — the invariant `fields.project.key` and the
    synthesized keys of keyless siblings are both built on."""
    from tests._helpers import build_corpus

    def rec(did, key):
        return complete(
            source_type="jira",
            doc_id=did,
            project="payments",
            title=did,
            content="c",
            author_email="ava@acme.com",
            key=key,
        )

    with pytest.raises(SystemExit) as e:
        build_corpus(tmp_path / "one", [rec("j-a", "BILL-2"), rec("j-b", "PAY-1")])
    assert "BILL" in str(e.value) and "PAY-1" in str(e.value)

    # Across runs too, through the seeded maps.
    settings = Settings(data_dir=tmp_path)
    first = tmp_path / "a.jsonl"
    first.write_text(json.dumps(rec("j-a", "BILL-2")))
    load(first, settings, reset=True)
    second = tmp_path / "b.jsonl"
    second.write_text(json.dumps(rec("j-b", "PAY-1")))
    with pytest.raises(SystemExit) as e:
        load(second, settings, reset=False)
    assert "BILL" in str(e.value) and "PAY-1" in str(e.value)


def test_byo_a_stated_id_is_served_verbatim_by_every_probed_source(tmp_path):
    """confluence and hubspot state their served id the way github states `number` and jira states
    `key` (`content_id` / `record_id`). Three properties, one corpus:

    a stated id is served verbatim; a keyless sibling probes around it rather than onto it; and a
    SECOND record asking for the same id is refused rather than replacing the first — the upsert's
    conflict target is that very id, so without the claim check it would `DO UPDATE` and lose a
    document at the id it was reachable by.

    The keyless sibling is what makes the "rather than onto it" half real: its own seed is free to
    collide with the stated value, and only assigning stated ids FIRST, corpus-wide, keeps it off.
    """
    from tests._helpers import build_corpus

    def page(doc_id, title, **extra):
        return {
            "source_type": "confluence",
            "space": "eng",
            "doc_id": doc_id,
            "title": title,
            "content": "c",
            "author_email": "a@acme.com",
            **extra,
        }

    s = build_corpus(
        tmp_path,
        [
            page("p1", "Stated", content_id=90001),
            page("p2", "Keyless"),
            {
                "source_type": "hubspot",
                "object_type": "companies",
                "doc_id": "h1",
                "title": "Stated Co",
                "content": "c",
                "author_email": "a@acme.com",
                "record_id": "7011234567",
            },
        ],
    )
    conn = store.connect_ro(s.db_path)
    pages = {r["title"]: r["id"] for r in conn.execute("SELECT title, id FROM confluence_pages")}
    assert pages["Stated"] == 90001
    assert pages["Keyless"] != 90001
    assert conn.execute("SELECT id FROM hubspot_objects").fetchone()["id"] == "7011234567"
    conn.close()

    # A second record asking for a taken id is refused, and the refusal names the holder.
    with pytest.raises(SystemExit, match="content_id 90001 is already claimed by 'p1'"):
        build_corpus(
            tmp_path / "clash",
            [page("p1", "Stated", content_id=90001), page("pX", "Clash", content_id=90001)],
        )


def test_byo_a_keyless_row_probes_off_a_stated_id_rather_than_onto_it(tmp_path, monkeypatch):
    """The half `test_byo_a_stated_id_is_served_verbatim_by_every_probed_source` cannot force on
    its own: there, the keyless page's own seed simply misses the stated value, so the assignment
    would look right even with the probe replaced by a bare seed.

    Collapsing the seed to the stated id makes the collision certain — every keyless page now WANTS
    90001 — so the only thing that can keep it off is assigning stated ids first and probing the
    rest against them. That is also why confluence and hubspot had to become deferred sources when
    they gained a stateable id (see byo.DEFERRED_ID)."""
    from backlot import store
    from tests._helpers import build_corpus

    monkeypatch.setitem(store.ID_SEED, "confluence", (lambda seed: 90001, None))

    def page(doc_id, title, **extra):
        return {
            "source_type": "confluence",
            "space": "eng",
            "doc_id": doc_id,
            "title": title,
            "content": "c",
            "author_email": "a@acme.com",
            **extra,
        }

    # The keyless pages sort BEFORE the stated one by dataset id, so a streaming probe would have
    # handed 90001 to `a-keyless` long before `z-stated` was ever read.
    s = build_corpus(
        tmp_path,
        [
            page("a-keyless", "Keyless one"),
            page("b-keyless", "Keyless two"),
            page("z-stated", "Stated", content_id=90001),
        ],
    )
    monkeypatch.undo()
    conn = store.connect_ro(s.db_path)
    try:
        got = {r["title"]: r["id"] for r in conn.execute("SELECT title, id FROM confluence_pages")}
        assert got["Stated"] == 90001, "the stated id lost its spelling to a keyless sibling"
        assert len(set(got.values())) == 3
        assert 90001 not in (got["Keyless one"], got["Keyless two"])
    finally:
        conn.close()


def test_byo_a_stated_id_already_held_by_an_earlier_import_is_refused(tmp_path):
    """The cross-run half of the claim. `seed_tracker_ids` preloads every PROBED source's stored
    keys, so an append stating an id a previous import already assigned is refused — the upsert's
    conflict target IS that id, so without the preload it would `DO UPDATE` and replace the row,
    losing a document at the id it was reachable by.

    Dropping confluence and hubspot from that preload leaves every other test in this suite green,
    because the append guard fires first for a record with NO id at all. Only a record that states
    a taken one reaches the check."""
    from backlot import store
    from backlot.config import Settings
    from backlot.importer.byo import load

    settings = Settings(data_dir=tmp_path)

    def corpus(name, **extra):
        p = tmp_path / name
        p.write_text(
            json.dumps(
                complete(
                    **{
                        "source_type": "confluence",
                        "space": "eng",
                        "title": name,
                        "content": "c",
                        "author_email": "a@acme.com",
                        **extra,
                    }
                )
            )
            + "\n"
        )
        return p

    load(corpus("first.jsonl", doc_id="p1", content_id=90001), settings, reset=True)
    with pytest.raises(SystemExit, match="already claimed by a previous import"):
        load(corpus("second.jsonl", doc_id="pX", content_id=90001), settings, reset=False)
    # ...and the refused append left the original page exactly where it was.
    conn = store.connect_ro(settings.db_path)
    try:
        assert [tuple(r) for r in conn.execute("SELECT id, title FROM confluence_pages")] == [
            (90001, "first.jsonl")
        ]
    finally:
        conn.close()


def test_byo_a_comment_id_follows_its_parents_settled_key(tmp_path):
    """A child row's own id is composed from its parent's SERVED key, and a deferred source's key
    is not settled until every record has been seen — so the comment written while its parent still
    held a provisional key has to be recomposed when the parent moves.

    Without that, a comment on a KEYLESS jira issue or confluence page was served under
    `-unassigned-3::c1`. It stayed hidden because every fixture that paired comments with a
    deferred parent happened to STATE that parent's key, in which case no provisional is ever used.
    Both halves are asserted here: the keyless parent is the one at risk, the stated one is the
    control.
    """
    from tests._helpers import build_corpus

    def issue(doc_id, title, **extra):
        return {
            "source_type": "jira",
            "project": "payments",
            "doc_id": doc_id,
            "title": title,
            "content": "c",
            "author_email": "a@acme.com",
            "comments": [{"content": "first", "author_email": "b@acme.com"}],
            **extra,
        }

    s = build_corpus(tmp_path, [issue("j1", "Keyless"), issue("j2", "Stated", key="PAY-7")])
    conn = store.connect_ro(s.db_path)
    try:
        keyed = {r["title"]: r["key"] for r in conn.execute("SELECT title, key FROM jira_issues")}
        comments = {r["key"]: r["id"] for r in conn.execute("SELECT key, id FROM jira_comments")}
        for title in ("Keyless", "Stated"):
            parent = keyed[title]
            assert comments[parent] == f"{parent}::c1", (
                f"{title} parent: the comment id did not follow its parent's settled key"
            )
        # ...and the provisional spelling reaches no row at all.
        assert not any("unassigned" in cid for cid in comments.values())
    finally:
        conn.close()


def test_byo_a_derived_number_no_longer_moves_across_append(tmp_path):
    """Appending must not touch a number an existing row already serves. A number free to move when
    `--append` changes the set renumbers a link a client had already saved.

    The appended row states its own `number`, which an append into a probed source requires (see
    `_require_provided_id`): a row's identity has to be stated once the dataset's own identifier
    does not outlive the import, so "append a keyless issue" is not a shape that exists.
    Re-importing a row across an append is refused outright —
    `test_github_comment_ids_are_unique_even_when_the_seed_collides` pins that.
    """
    from backlot import store
    from backlot.importer.byo import load

    def rec(did, **extra):
        return complete(
            source_type="github",
            doc_id=did,
            repo="core",
            title=did,
            content="c",
            author_email="ava@acme.com",
            **extra,
        )

    settings = Settings(data_dir=tmp_path)
    shard1 = tmp_path / "s1.jsonl"
    shard1.write_text("\n".join(json.dumps(rec(d)) for d in ("gh-000000", "gh-054074")))
    load(shard1, settings, reset=True)

    conn = store.connect_ro(settings.db_path)
    before = {
        r["title"]: r["number"] for r in conn.execute("SELECT title, number FROM github_items")
    }
    conn.close()
    assert set(before) == {"gh-000000", "gh-054074"}

    shard2 = tmp_path / "s2.jsonl"
    # A number no existing row holds, so the append is a pure addition.
    fresh = max(before.values()) + 1
    shard2.write_text(json.dumps(rec("gh-014031", number=fresh)))
    load(shard2, settings, reset=False)

    conn = store.connect_ro(settings.db_path)
    after = {
        r["title"]: r["number"] for r in conn.execute("SELECT title, number FROM github_items")
    }
    conn.close()
    assert after == {**before, "gh-014031": fresh}, "an append renumbered an existing row"


def test_byo_a_provider_appended_in_a_later_batch_does_not_abort_the_import(tmp_path):
    """An appended row claiming a number an existing row already serves must be refused, not
    written. Writing it duplicates a live value: the claim lands while the row holding that number
    -- untouched by THIS run, so still carrying it in the live table -- still holds it too.

    Needs no monkeypatching: `a-victim` is loaded alone first (it takes
    `synth.github_number("a-victim")` outright, nothing else in the repo to collide with), then a
    SECOND batch (`--append`) states that exact number for a different record. `a-victim` is never
    touched by the second batch's insert, so it is only visible through seed_tracker_ids'
    preload."""
    from backlot import synth
    from backlot.importer.byo import load

    stolen = synth.github_number("a-victim")
    shard1 = tmp_path / "s1.jsonl"
    shard1.write_text(
        json.dumps(
            complete(
                **{
                    "source_type": "github",
                    "doc_id": "a-victim",
                    "repo": "core",
                    "title": "v",
                    "content": "v",
                    "author_email": "ava@acme.com",
                }
            )
        )
    )
    settings = Settings(data_dir=tmp_path)
    load(shard1, settings, reset=True)
    conn = store.connect_ro(settings.db_path)
    before = conn.execute("SELECT number FROM github_items").fetchone()["number"]
    assert before == stolen  # sanity: nothing to collide with yet, so it got its own hash
    conn.close()

    shard2 = tmp_path / "s2.jsonl"
    shard2.write_text(
        json.dumps(
            complete(
                **{
                    "source_type": "github",
                    "doc_id": "z-provider",
                    "repo": "core",
                    "title": "p",
                    "content": "p",
                    "author_email": "ava@acme.com",
                    "number": stolen,
                }
            )
        )
    )
    # An appended row claiming a number an existing row already serves is refused rather than
    # displacing it: the product imports a dataset once and serves it read-only, so blocking a rare
    # colliding append beats renumbering a row a client may hold a link to. The message names the
    # VALUE, not the holder -- that row has no corpus identifier left to name it by.
    with pytest.raises(SystemExit, match="already claimed by a previous import in repo 'core'"):
        load(shard2, settings, reset=False)

    # ...and the refusal leaves the DB untouched: one commit for the whole import, so the
    # existing row keeps the number it served before the failed append.
    conn = store.connect_ro(settings.db_path)
    served = {
        r["title"]: r["number"] for r in conn.execute("SELECT title, number FROM github_items")
    }
    conn.close()
    assert served == {"v": stolen}


def test_byo_a_jira_provider_appended_in_a_later_batch_is_refused(tmp_path):
    """jira's own version of the case just above. `j-victim` is loaded alone first (it takes
    `synth.jira_key_number("j-victim")` outright, nothing else in the project to collide with), then
    a SECOND batch (`--append`) states a key carrying that exact suffix for a different record.
    `j-victim` is never touched by the second batch's insert, so it is only visible through
    seed_tracker_ids' preload."""
    from backlot import synth
    from backlot.importer.byo import load

    stolen = synth.jira_key_number("j-victim")
    shard1 = tmp_path / "s1.jsonl"
    shard1.write_text(
        json.dumps(
            complete(
                **{
                    "source_type": "jira",
                    "doc_id": "j-victim",
                    "project": "payments",
                    "title": "v",
                    "content": "v",
                    "author_email": "ava@acme.com",
                }
            )
        )
    )
    settings = Settings(data_dir=tmp_path)
    load(shard1, settings, reset=True)
    conn = store.connect_ro(settings.db_path)
    before = conn.execute("SELECT key FROM jira_issues").fetchone()["key"]
    # sanity: nothing to collide with yet, so it composed its own hash under the project's
    # synthesized prefix (no provided key in this shard to establish one)
    assert before.endswith(f"-{stolen}")
    conn.close()

    shard2 = tmp_path / "s2.jsonl"
    shard2.write_text(
        json.dumps(
            complete(
                **{
                    "source_type": "jira",
                    "doc_id": "z-provider",
                    "project": "payments",
                    "title": "p",
                    "content": "p",
                    "author_email": "ava@acme.com",
                    "key": f"PAY-{stolen}",
                }
            )
        )
    )
    # Same trade as github's: with ONE `key` column there is no provided-vs-served distinction to
    # arbitrate, so an appended row claiming a key an existing row already serves is refused rather
    # than displacing it.
    #
    # It is refused for a sharper reason than the suffix collision: a COMPOSED key is a real key, so
    # it registers its project's prefix exactly as a provided one does. `j-victim` already named
    # this project `PAYDF384A`, and the appended `PAY-3227` would rename it -- the prefix-holder
    # guard catches that first. So a project's prefix is settled by whatever it first served, the
    # same "ids do not move" rule as everywhere else.
    with pytest.raises(SystemExit, match="but its keys already name it 'PAYDF384A'"):
        load(shard2, settings, reset=False)

    # ...and the refusal leaves the DB untouched -- one commit for the whole import.
    conn = store.connect_ro(settings.db_path)
    served = {r["title"]: r["key"] for r in conn.execute("SELECT title, key FROM jira_issues")}
    conn.close()
    assert served == {"v": before}


def test_byo_a_notion_parent_must_name_an_imported_page(tmp_path):
    """notion was the one hierarchy source that stored a parent without checking it: its id is a
    pure hash, which answers for a page that does not exist as readily as for one that does, so the
    page served a `parent` UUID that 404s and `children` never listed it. confluence and jira refuse
    the identical shape."""
    page = {
        "source_type": "notion",
        "subtype": "page",
        "teamspace": "eng",
        "content": "c",
        "author_email": "ava@acme.com",
    }
    with pytest.raises(SystemExit, match="names no imported page"):
        load(
            _write(tmp_path, [{**page, "doc_id": "n-kid", "title": "Kid", "parent": "nobody"}]),
            Settings(data_dir=tmp_path),
        )
    # ...and one whose parent IS imported loads, including a parent from an earlier run: a notion id
    # survives the import that wrote it, so unlike confluence's key it still resolves across shards.
    settings = Settings(data_dir=tmp_path)
    load(
        _write(tmp_path, [{**page, "doc_id": "n-db", "title": "DB", "subtype": "database"}]),
        settings,
    )
    load(
        _write(
            tmp_path,
            [{**page, "doc_id": "n-kid", "title": "Kid", "parent": "n-db"}],
            name="s2.jsonl",
        ),
        settings,
        reset=False,
    )
    conn = store.connect_ro(settings.db_path)
    assert conn.execute("SELECT parent_id FROM notion_pages WHERE title = 'Kid'").fetchone()[0] == (
        served_id("notion", "n-db")
    )
    conn.close()


def test_byo_a_failed_fresh_import_leaves_the_previous_corpus_in_place(tmp_path):
    """A fresh load replaces a corpus, and it did so by deleting the old DB before reading a single
    record — so a typo left an operator with no corpus at all: an empty schema-only DB and a
    tokens.yaml still describing the one that was gone. An --append is already all-or-nothing."""
    settings = Settings(data_dir=tmp_path)
    good = {
        "source_type": "jira",
        "doc_id": "j-1",
        "project": "payments",
        "title": "Keep me",
        "content": "c",
        "author_email": "ava@acme.com",
    }
    load(_write(tmp_path, [good]), settings)
    before = _dump_tables(settings.db_path)
    tokens_before = settings.tokens_path.read_text()

    with pytest.raises(SystemExit):
        load(
            _write(
                tmp_path,
                [
                    {**good, "doc_id": "j-2", "title": "T"},
                    {**good, "doc_id": "j-3", "created": "nope"},
                ],
                name="bad.jsonl",
            ),
            settings,
        )
    assert _dump_tables(settings.db_path) == before
    assert settings.tokens_path.read_text() == tokens_before
    assert not list(tmp_path.glob("*.replaced"))  # and no debris beside it


def test_byo_an_orphan_review_comment_anchor_is_reported(tmp_path, capsys):
    """A review comment whose `path` names no file document is served nowhere — dropped from
    `/pulls/{n}/comments` and 404 at `/pulls/comments/{id}`, so the response cannot reveal which
    paths exist — and it left no trace at import, so the comment simply vanished. It reaches the
    same report `changed_paths` does: a count on a load, one named line under `--dry-run`."""
    corpus = _write(
        tmp_path,
        _gh_changeset_corpus(
            changed_paths=["src/a.py"],
            comments=[
                {
                    "content": "on a file that is not here",
                    "author_email": "b@x.com",
                    "path": "src/typo.py",
                    "line": 1,
                }
            ],
        ),
    )
    assert byo.run(corpus, dry_run=True) == 0  # a report, not a verdict on the corpus
    dry = capsys.readouterr().err
    assert "1 path reference" in dry and "src/typo.py" in dry and "review comment" in dry
    load(corpus, Settings(data_dir=tmp_path))
    assert "1 path reference" in capsys.readouterr().err


# --- what an --append may and may not do to a document already imported -------------------


def _gh_file(doc_id, path, **extra):
    """A github file row. Its clock is distinct per `doc_id` unless the caller states one: two
    snapshots of a path are told apart by `created`, so a shared second is a different test."""
    import hashlib

    extra.setdefault(
        "created",
        1_770_000_000 + int(hashlib.sha256(doc_id.encode()).hexdigest()[:6], 16) % 5_000_000,
    )
    fields = {
        "doc_id": doc_id,
        "repo": "gw",
        "subtype": "file",
        "path": path,
        "title": path.rsplit("/", 1)[-1],
        "content": "a\nb\n",
        "author_email": "ava@acme.com",
        **extra,
    }
    return complete("github", **fields)


def test_byo_two_snapshots_of_one_file_both_load(tmp_path):
    """A file the corpus states twice at different times is that file's HISTORY, not two files.

    Both rows load and keep their own content; the file is SERVED at its newest snapshot. Before
    this, the second row adopted the first's number (a file is looked up by path) and the
    corpus-wide identity check refused the corpus outright, so a document set that recorded a
    file's edits was unloadable.
    """
    settings = Settings(data_dir=tmp_path)
    corpus = tmp_path / "c.jsonl"
    corpus.write_text(
        "\n".join(
            json.dumps(r)
            for r in [
                _gh_file("gh-old", "src/a.py", content="v1\n", created="2025-01-01T00:00:00+00:00"),
                _gh_file("gh-new", "src/a.py", content="v2\n", created="2025-06-01T00:00:00+00:00"),
            ]
        )
    )
    load(corpus, settings)
    conn = store.connect_ro(settings.db_path)
    rows = conn.execute(
        "SELECT number, content FROM github_items WHERE repo='gw' AND path='src/a.py' "
        "AND kind='file' ORDER BY created_ts"
    ).fetchall()
    assert [r["content"] for r in rows] == ["v1\n", "v2\n"]
    assert len({r["number"] for r in rows}) == 2  # each snapshot is addressable in its own right
    assert store.get_repo_file(conn, "gw", "src/a.py")["content"] == "v2\n"  # HEAD
    conn.close()


def test_byo_snapshots_ordered_by_a_synthesized_clock_are_reported(tmp_path, capsys):
    """A path whose snapshots state no `created` loads, and says so.

    With no `created` the clock is `synth.epoch(doc_id)` — a stable fake, but one that orders
    snapshots by a hash of their dataset ids, so which one the file is SERVED at is not something
    the corpus chose. This was a hard refusal before snapshots existed, and neither the load nor
    `--dry-run` can see it any other way, so it gets the same one-line notice an unresolved
    `changed_paths` gets rather than passing in silence.
    """
    settings = Settings(data_dir=tmp_path)
    corpus = tmp_path / "c.jsonl"
    corpus.write_text(
        "\n".join(
            json.dumps(r)
            for r in [
                _gh_file("gh-old-version", "src/a.py", content="v1\n"),
                _gh_file("gh-new-version", "src/a.py", content="v2\n"),
                # a path with ONE snapshot needs no clock and must not be reported
                _gh_file("gh-solo", "src/b.py", content="only\n"),
            ]
        )
    )
    load(corpus, settings)
    err = capsys.readouterr().err
    assert "src/a.py" in err or "1 file path" in err
    assert "created" in err
    assert "src/b.py" not in err


def test_byo_two_file_rows_at_one_instant_are_refused_naming_the_path(tmp_path):
    """Two DIFFERENT documents claiming one file at the same instant are still a conflict — there
    is no order that makes one of them HEAD — and the message must name `path`.

    `number` is ignored for a file row, so the generic "give one of them a different id" advice
    cannot be acted on here; it sent a reader looking at synthesized numbers instead of the field
    actually in conflict.
    """
    settings = Settings(data_dir=tmp_path)
    corpus = tmp_path / "c.jsonl"
    corpus.write_text(
        "\n".join(
            json.dumps(r)
            for r in [
                _gh_file("gh-1", "src/a.py", content="v1\n", created="2025-01-01T00:00:00+00:00"),
                # a different number cannot separate them: a file is addressed by (repo, path)
                _gh_file(
                    "gh-2",
                    "src/a.py",
                    content="v2\n",
                    created="2025-01-01T00:00:00+00:00",
                    number=4321,
                ),
            ]
        )
    )
    with pytest.raises(SystemExit) as exc:
        load(corpus, settings)
    msg = str(exc.value)
    assert "src/a.py" in msg
    assert "gh-1" in msg
    assert "ref" in msg  # points at the field that WOULD separate them


def test_byo_two_file_rows_sharing_a_ref_are_told_to_change_the_ref(tmp_path):
    """Rows that already state one `ref` are told to change the REF, not the `created`.

    `ref` wins over `created` in the snapshot key, so an author who follows "or its own `created`"
    here re-imports and hits the identical error.
    """
    settings = Settings(data_dir=tmp_path)
    corpus = tmp_path / "c.jsonl"
    corpus.write_text(
        "\n".join(
            json.dumps(r)
            for r in [
                _gh_file(
                    "gh-1",
                    "src/a.py",
                    content="v1\n",
                    created="2025-01-01T00:00:00+00:00",
                    ref="pr-9",
                ),
                _gh_file(
                    "gh-2",
                    "src/a.py",
                    content="v2\n",
                    created="2025-06-01T00:00:00+00:00",
                    ref="pr-9",
                ),
            ]
        )
    )
    with pytest.raises(SystemExit) as exc:
        load(corpus, settings)
    msg = str(exc.value)
    assert "different `ref`" in msg
    assert "will not separate them" in msg


def test_byo_a_ref_on_a_row_that_is_not_a_file_is_refused(tmp_path):
    """`ref` names which snapshot of a path a row is, so it means nothing on an issue or a pull.

    Stored and ignored is the failure this check exists to prevent — the same reason
    `changed_paths` on a non-pull is an error rather than a no-op: the corpus author gets no signal
    that the field they wrote is not the one being served.
    """
    settings = Settings(data_dir=tmp_path)
    corpus = tmp_path / "c.jsonl"
    corpus.write_text(
        json.dumps(
            complete(
                **{
                    "source_type": "github",
                    "doc_id": "gh-i",
                    "repo": "gw",
                    "title": "Bug",
                    "content": "x",
                    "author_email": "ava@acme.com",
                    "ref": "pr-11",
                }
            )
        )
    )
    with pytest.raises(SystemExit) as exc:
        load(corpus, settings)
    assert "ref" in str(exc.value)
    assert "file" in str(exc.value)


def test_byo_a_stated_ref_separates_two_snapshots_sharing_an_instant(tmp_path):
    """`ref` is how a corpus says "a different point in time" when the clock cannot: two snapshots
    at one `created` are distinct when they name different refs."""
    settings = Settings(data_dir=tmp_path)
    corpus = tmp_path / "c.jsonl"
    corpus.write_text(
        "\n".join(
            json.dumps(r)
            for r in [
                _gh_file(
                    "gh-1",
                    "src/a.py",
                    content="v1\n",
                    created="2025-01-01T00:00:00+00:00",
                    ref="pr-11",
                ),
                _gh_file(
                    "gh-2",
                    "src/a.py",
                    content="v2\n",
                    created="2025-01-01T00:00:00+00:00",
                    ref="pr-12",
                ),
            ]
        )
    )
    load(corpus, settings)
    conn = store.connect_ro(settings.db_path)
    rows = conn.execute(
        "SELECT ref, content FROM github_items WHERE repo='gw' AND path='src/a.py' AND kind='file'"
        " ORDER BY ref"
    ).fetchall()
    assert [(r["ref"], r["content"]) for r in rows] == [("pr-11", "v1\n"), ("pr-12", "v2\n")]
    conn.close()


def test_byo_a_github_file_is_appendable_and_keeps_its_number(tmp_path):
    """A file states its identity in full as (repo, path), so an --append needs no `number` from
    it — and cannot use one, since the schema says a file's number is ignored. Requiring one made
    every sharded github corpus containing files unloadable past the first shard, which is exactly
    the layout this importer's own changeset report tells authors to use.

    Its number still cannot move: a re-imported file lands back on the row that path occupies."""
    settings = Settings(data_dir=tmp_path)
    first = tmp_path / "s1.jsonl"
    first.write_text(
        "\n".join(
            json.dumps(r)
            for r in [
                complete(
                    **{
                        "source_type": "github",
                        "doc_id": "gh-issue",
                        "repo": "gw",
                        "title": "Bug",
                        "content": "x",
                        "author_email": "ava@acme.com",
                        "number": 7,
                    }
                ),
                _gh_file("gh-a", "src/a.py"),
            ]
        )
    )
    load(first, settings)
    conn = store.connect_ro(settings.db_path)
    a_number = conn.execute("SELECT number FROM github_items WHERE path = 'src/a.py'").fetchone()[0]
    conn.close()

    second = tmp_path / "s2.jsonl"
    second.write_text(
        "\n".join(
            json.dumps(r) for r in [_gh_file("gh-b", "src/b.py"), _gh_file("gh-a", "src/a.py")]
        )
    )
    load(second, settings, reset=False)
    conn = store.connect_ro(settings.db_path)
    files = {
        r["path"]: r["number"]
        for r in conn.execute("SELECT path, number FROM github_items WHERE kind = 'file'")
    }
    conn.close()
    assert set(files) == {"src/a.py", "src/b.py"}
    assert files["src/a.py"] == a_number  # the re-imported file did not renumber
    assert files["src/b.py"] not in (a_number, 7)  # nor did it take a number in use


def test_byo_a_re_imported_slack_message_is_recognised_not_duplicated(tmp_path):
    """Nothing in a slack record states a ts, so a re-imported message can only be recognised by
    what it says: same channel, same second, same author, same text. Without that the probe found
    its own previous row holding the deterministic ts, walked to a free fraction, and imported the
    message a second time — silently, with no field a corpus could set to opt out.

    Two such records in ONE corpus stay two documents: they are two things the author wrote, and
    only a value already in the DB before the run can be a re-import."""
    rec = {
        "source_type": "slack",
        "doc_id": "s-1",
        "channel": "incidents",
        "content": "deploy is green",
        "author_email": "ava@acme.com",
        "created": 1000,
    }
    settings = Settings(data_dir=tmp_path)
    corpus = _write(tmp_path, [rec])
    load(corpus, settings)
    load(corpus, settings, reset=False)
    conn = store.connect_ro(settings.db_path)
    assert conn.execute("SELECT COUNT(*) FROM slack_messages").fetchone()[0] == 1
    conn.close()

    twins = _write(tmp_path, [rec, {**rec, "doc_id": "s-2"}], name="twins.jsonl")
    load(twins, settings, reset=False)
    conn = store.connect_ro(settings.db_path)
    assert conn.execute("SELECT COUNT(*) FROM slack_messages").fetchone()[0] == 2
    conn.close()


@pytest.mark.parametrize(
    "first, second",
    [
        pytest.param(
            {
                "source_type": "fireflies",
                "doc_id": "ff-1",
                "channel": "meetings",
                "title": "Standup",
                "content": "A: one",
                "author_email": "ava@acme.com",
                "readers": ["ava@acme.com"],
                "transcript_id": "01JAAAAAAAAAAAAAAAAAAAAAAA",
                "sentences": [{"content": "one", "speaker_name": "A"}],
            },
            {
                "doc_id": "ff-2",
                "title": "Retro",
                "author_email": "bob@acme.com",
                "readers": ["bob@acme.com"],
            },
            id="fireflies-transcript_id",
        ),
        pytest.param(
            {
                "source_type": "s3",
                "doc_id": "s3-1",
                "bucket": "acme-data",
                "key": "reports/q1.csv",
                "title": "q1",
                "content": "v1",
                "author_email": "ava@acme.com",
                "readers": ["ava@acme.com"],
            },
            {
                "doc_id": "s3-2",
                "content": "v2",
                "author_email": "bob@acme.com",
                "readers": ["bob@acme.com"],
            },
            id="s3-bucket-key",
        ),
    ],
)
def test_byo_a_stated_id_an_earlier_import_holds_is_refused(tmp_path, first, second):
    """The claim the four DEFERRED_ID sources get corpus-wide, for the two that assign no id of
    their own. A stated id landing on a document a previous import wrote cannot be told apart from
    that document coming back, so replacing it silently traded one document for another — and the
    old document's readers stayed granted on the new one, because grants were append-only."""
    settings = Settings(data_dir=tmp_path)
    load(_write(tmp_path, [first]), settings)
    src = first["source_type"]
    with pytest.raises(SystemExit, match="an EARLIER import already answers at"):
        load(_write(tmp_path, [{**first, **second}], name="s2.jsonl"), settings, reset=False)
    conn = store.connect_ro(settings.db_path)
    assert [r["title"] for r in conn.execute(f"SELECT title FROM {store.table(src)}")] == [
        first["title"]
    ]
    assert [
        r["principal_id"] for r in conn.execute(f"SELECT principal_id FROM {store.acl_table(src)}")
    ] == first["readers"]
    conn.close()


@pytest.mark.parametrize(
    "extra",
    [{"project": "payments"}, {"repo": "gw"}, {"space": "handbook"}, {"channel": "inc"}],
    ids=["jira", "github", "confluence", "slack"],
)
def test_byo_two_records_sharing_a_dataset_id_are_one_document(tmp_path, extra):
    """A `doc_id` the corpus repeats names ONE document, and a real corpus does repeat one (a jira
    pair in the ERB bench). That held while the dataset id WAS the key; once a probed source
    started handing the second record a fresh id, the same corpus imported two documents — and the
    parent, the comments and the grants split between them.

    The last record wins the row, which is what a direct import of the same documents produces."""
    source_type = {"project": "jira", "repo": "github", "space": "confluence", "channel": "slack"}[
        next(iter(extra))
    ]
    base = {
        "source_type": source_type,
        "doc_id": "dup",
        "content": "one",
        "author_email": "ava@acme.com",
        **extra,
    }
    if source_type != "slack":  # slack messages carry no title
        base["title"] = "first"
    settings = Settings(data_dir=tmp_path)
    # `content`, not `title`, is what tells the two apart: it is the one field every source has
    load(_write(tmp_path, [base, {**base, "content": "two"}]), settings)
    conn = store.connect_ro(settings.db_path)
    rows = conn.execute(f"SELECT content FROM {store.table(source_type)}").fetchall()
    conn.close()
    assert [r["content"] for r in rows] == ["two"]


def test_byo_a_parent_declared_on_a_repeated_dataset_id_reaches_the_row(tmp_path):
    """The consequence of the rule above that a bare row count cannot see: with two rows, `parent`
    resolved through a last-writer-wins map and attached to whichever row the map ended on — so
    the record that DECLARED a parent served none, and the one that declared none served it."""
    page = {
        "source_type": "confluence",
        "space": "handbook",
        "author_email": "ava@acme.com",
        "content": "c",
    }
    settings = Settings(data_dir=tmp_path)
    load(
        _write(
            tmp_path,
            [
                {**page, "doc_id": "parent", "title": "Parent"},
                {**page, "doc_id": "kid", "title": "declares", "parent": "parent"},
                {**page, "doc_id": "kid", "title": "silent"},
            ],
        ),
        settings,
    )
    conn = store.connect_ro(settings.db_path)
    rows = {
        r["title"]: r["parent_id"]
        for r in conn.execute("SELECT title, parent_id FROM confluence_pages")
    }
    parent_id = conn.execute("SELECT id FROM confluence_pages WHERE title = 'Parent'").fetchone()[0]
    conn.close()
    assert set(rows) == {"Parent", "silent"}  # one child row, the later record's
    assert rows["silent"] == parent_id


def test_byo_a_shorter_version_of_a_document_drops_the_children_it_lost(tmp_path):
    """Children are written under sequence ids, so a version with fewer of them overwrote 1..n and
    left the old tail: a transcript served one sentence of content beside three stored ones,
    breaking this importer's own rule that a transcript's content IS its sentences. A gmail thread
    kept removed messages the same way, and those are full documents — their ACL grants and their
    FTS rows went with them."""
    settings = Settings(data_dir=tmp_path)
    long_ff = {
        "source_type": "fireflies",
        "doc_id": "ff-x",
        "channel": "meetings",
        "title": "Standup",
        "content": "A: one\nA: two\nA: three",
        "author_email": "ava@acme.com",
        "sentences": [{"content": c, "speaker_name": "A"} for c in ("one", "two", "three")],
    }
    long_gm = {
        "source_type": "gmail",
        "doc_id": "gm-x",
        "title": "Re: deploy",
        "content": "root",
        "author_email": "ava@acme.com",
        "messages": [{"content": m, "author_email": "bob@acme.com"} for m in ("m1", "m2")],
    }
    load(_write(tmp_path, [long_ff, long_gm]), settings)
    short = [
        {**long_ff, "content": "A: one", "sentences": [{"content": "one", "speaker_name": "A"}]},
        {**long_gm, "messages": [{"content": "m1", "author_email": "bob@acme.com"}]},
    ]
    load(_write(tmp_path, short, name="s2.jsonl"), settings, reset=False)
    conn = store.connect_ro(settings.db_path)
    assert [
        r["body"] for r in conn.execute("SELECT body FROM fireflies_sentences ORDER BY seq")
    ] == ["one"]
    assert [
        r["content"] for r in conn.execute("SELECT content FROM gmail_messages ORDER BY thread_seq")
    ] == ["root", "m1"]
    # the dropped message took its grants with it, or it stays readable at an id nothing serves
    assert conn.execute(f"SELECT COUNT(*) FROM {store.acl_table('gmail')}").fetchone()[0] == 2
    conn.close()


def test_byo_keyless_linear_identifiers_are_probed_not_hashed(tmp_path):
    """`synth.linear_identifier` numbers within 9,000 values per team, so a plain hash collides by
    the birthday bound at ~110 keyless issues in one team — and two issues sharing `ENG-2686` leaves
    one unreachable at the only human-facing id it advertises, since `issue(id:)` answers the first.

    120 issues, the smallest count these seeds collide at. Asserted rather than assumed: without it
    a future change to the seed could leave the corpus collision-free and the test vacuous."""
    settings = Settings(data_dir=tmp_path)
    load(
        _write(
            tmp_path,
            [
                {
                    "source_type": "linear",
                    "doc_id": f"li-{i}",
                    "team": "engineering",
                    "title": f"T{i}",
                    "content": "c",
                    "author_email": "ava@acme.com",
                }
                for i in range(120)
            ],
        ),
        settings,
    )
    conn = store.connect_ro(settings.db_path)
    ids = [r["identifier"] for r in conn.execute("SELECT identifier FROM linear_issues")]
    conn.close()
    assert len(ids) == 120 and len(set(ids)) == 120
    # the corpus really does collide under a plain hash, or the assertion above proves nothing
    team = synth.linear_team_key("engineering")
    hashed = [synth.linear_identifier(f"li-{i}", team) for i in range(120)]
    assert len(set(hashed)) < 120


# --- github pull changesets and review comments -----------------------------------------


def _gh_changeset_corpus(**pr_extra):
    """A repo of two files plus one PR, with whatever the caller wants on the PR."""
    files = [
        {
            "source_type": "github",
            "doc_id": f"cs-file-{i}",
            "repo": "cs",
            "subtype": "file",
            "path": path,
            "title": path,
            "content": "a\nb\nc\n",
            "author_email": "a@x.com",
        }
        for i, path in enumerate(["src/a.py", "src/b.py"])
    ]
    return files + [
        {
            "source_type": "github",
            "doc_id": "cs-pr",
            "repo": "cs",
            "subtype": "pull_request",
            "title": "A pull",
            "content": "body",
            "author_email": "a@x.com",
            **pr_extra,
        }
    ]


def test_github_changeset_fields_round_trip(tmp_path):
    """`changed_paths` says which files a pull touched — without it the router picks
    deterministically, which is well-formed but unrelated to what the pull is about. A comment
    carrying `path` is a line-anchored REVIEW comment; one without is a conversation comment. Both
    live in github_comments, discriminated by `path`."""
    load(
        _write(
            tmp_path,
            _gh_changeset_corpus(
                changed_paths=["src/b.py", "src/a.py"],
                comments=[
                    {"content": "overall looks fine", "author_email": "b@x.com"},
                    {
                        "content": "this line is dead",
                        "author_email": "b@x.com",
                        "path": "src/a.py",
                        "line": 2,
                    },
                    {
                        "content": "file-level note",
                        "author_email": "b@x.com",
                        "path": "src/a.py",
                        "diff_hunk": "@@ -1,2 +1,2 @@\n a\n-b\n",
                    },
                ],
            ),
        ),
        Settings(data_dir=tmp_path),
    )
    conn = store.connect_ro(tmp_path / "mock.sqlite")
    row = conn.execute("SELECT * FROM github_items WHERE kind = 'pull_request'").fetchone()
    assert store.jcol(row, "changed_paths") == ["src/b.py", "src/a.py"]  # order preserved
    f0 = conn.execute("SELECT * FROM github_items WHERE kind = 'file' AND path = ?", ("src/a.py",))
    assert f0.fetchone()["changed_paths"] is None

    assert [
        c["body"] for c in store.github_comments(conn, row["repo"], row["number"], anchored=False)
    ] == ["overall looks fine"]
    anchored = store.github_comments(conn, row["repo"], row["number"], anchored=True)
    assert [(c["path"], c["line"]) for c in anchored] == [("src/a.py", 2), ("src/a.py", None)]
    assert anchored[1]["diff_hunk"].startswith("@@ -1,2 +1,2 @@")


@pytest.mark.parametrize(
    "records, expected",
    [
        # a changeset and a review comment are both pull-only; on an issue they would be stored and
        # then be unservable
        (
            lambda: [
                {
                    "source_type": "github",
                    "doc_id": "cs-issue",
                    "repo": "cs",
                    "title": "An issue",
                    "content": "body",
                    "author_email": "a@x.com",
                    "changed_paths": ["src/a.py"],
                }
            ],
            "changed_paths",
        ),
        (
            lambda: [
                {
                    "source_type": "github",
                    "doc_id": "cs-issue",
                    "repo": "cs",
                    "title": "An issue",
                    "content": "body",
                    "author_email": "a@x.com",
                    "comments": [{"content": "x", "author_email": "b@x.com", "path": "src/a.py"}],
                }
            ],
            "review comment",
        ),
        # `changed_paths` is a list of paths, not one path
        (lambda: _gh_changeset_corpus(changed_paths="src/a.py"), "changed_paths"),
        # `path` is what marks a comment as line-anchored, so line/diff_hunk without one would be
        # served by neither endpoint
        (
            lambda: _gh_changeset_corpus(
                comments=[{"content": "where?", "author_email": "b@x.com", "line": 3}]
            ),
            "path",
        ),
    ],
    ids=[
        "changed_paths-on-issue",
        "review-comment-on-issue",
        "changed_paths-not-a-list",
        "no-path",
    ],
)
def test_github_changeset_misuse_is_refused(tmp_path, records, expected, capsys):
    corpus = _write(tmp_path, records())
    with pytest.raises(SystemExit) as e:
        load(corpus, Settings(data_dir=tmp_path))
    assert expected in str(e.value)
    # `--dry-run` reports what a load refuses, or a corpus passes the check that exists to spare
    # the author a failed import and then fails the import.
    assert byo.run(corpus, dry_run=True) == 1
    assert expected in capsys.readouterr().err


def test_a_changed_path_matching_no_file_is_reported_and_loaded(tmp_path, capsys):
    """A declared path that names no `file` document is a corpus typo the mock cannot prove is one:
    a corpus is routinely a SLICE of a repo, and under `--append` the file may land in a later
    shard. So it is REPORTED — naming the pull and the path — and loaded verbatim, leaving the
    router to drop it from the changeset. Refusing would make a pull that states the files it
    really touched unimportable whenever the slice stopped short of one of them."""
    corpus = _write(tmp_path, _gh_changeset_corpus(changed_paths=["src/a.py", "src/typo.py"]))

    assert byo.run(corpus, dry_run=True) == 0  # a report, not a verdict on the corpus
    dry = capsys.readouterr()
    assert "OK: 3 records valid." in dry.out
    assert "path reference" in dry.err and "line 3: src/typo.py" in dry.err
    assert "src/a.py" not in dry.err  # only the unresolved one is named

    # A load counts them and points at the report, rather than printing one line per path in among
    # its own ten lines of summary — the same split as a dangling linear `parent`.
    load(corpus, Settings(data_dir=tmp_path))
    err = capsys.readouterr().err
    assert "1 path reference" in err and "--dry-run" in err
    conn = store.connect_ro(tmp_path / "mock.sqlite")
    # stored as the corpus stated it — the report does not edit the record
    row = conn.execute("SELECT * FROM github_items WHERE kind = 'pull_request'").fetchone()
    assert store.jcol(row, "changed_paths") == ["src/a.py", "src/typo.py"]


# --- the --id-map manifest ----------------------------------------------------------------


_ID_MAP_CORPUS = [
    complete(
        **{
            "source_type": "github",
            "doc_id": "gh-stated",
            "repo": "acme/app",
            "number": 7,
            "title": "stated",
            "content": "a",
            "author_email": "a@x.com",
        }
    ),
    complete(
        **{
            "source_type": "github",
            "doc_id": "gh-keyless",
            "repo": "acme/app",
            "title": "keyless",
            "content": "b",
            "author_email": "a@x.com",
        }
    ),
    complete(
        **{
            "source_type": "jira",
            "doc_id": "j1",
            "project": "payments",
            "title": "t",
            "content": "c",
            "author_email": "a@x.com",
        }
    ),
    complete(
        **{
            "source_type": "slack",
            "doc_id": "s1",
            "channel": "general",
            "content": "hello",
            "author_email": "a@x.com",
            "created": "2024-01-02T03:04:05Z",
        }
    ),
    complete(
        **{
            "source_type": "s3",
            "doc_id": "o1",
            "bucket": "eng",
            "key": "docs/readme.md",
            "title": "readme",
            "content": "d",
            "author_email": "a@x.com",
        }
    ),
    complete(
        **{
            "source_type": "linear",
            "doc_id": "l1",
            "team": "engineering",
            "title": "t",
            "content": "e",
            "author_email": "a@x.com",
        }
    ),
    complete(
        **{
            "source_type": "google_drive",
            "doc_id": "d1",
            "folder": "Design",
            "title": "spec",
            "content": "f",
            "author_email": "a@x.com",
        }
    ),
    complete(
        **{
            "source_type": "confluence",
            "doc_id": "c1",
            "space": "ENG",
            "title": "page",
            "content": "g",
            "author_email": "a@x.com",
        }
    ),
    # doc_id omitted on purpose: the manifest must key it by the DEFAULTED dataset id.
    complete(
        **{
            "source_type": "gmail",
            "mailbox": "a@x.com",
            "title": "mail",
            "content": "h",
            "author_email": "a@x.com",
        }
    ),
]


def _load_with_map(tmp_path, records, name="c.jsonl", reset=True, settings=None):
    settings = settings or Settings(data_dir=tmp_path)
    out = tmp_path / f"{name}.idmap.json"
    load(_write(tmp_path, records, name), settings, reset=reset, id_map=out)
    return settings, json.loads(out.read_text())


def test_every_document_lands_in_the_id_map_under_its_served_key(tmp_path):
    settings, manifest = _load_with_map(tmp_path, _ID_MAP_CORPUS)
    docs = manifest["documents"]

    # One entry per record, keyed by the dataset id — including the defaulted one.
    assert sum(len(v) for v in docs.values()) == len(_ID_MAP_CORPUS)
    (gmail_did,) = docs["gmail"]
    assert gmail_did.startswith("dsid_")

    # Each entry spells the row's full served key, in the source's own id columns.
    conn = sqlite3.connect(settings.db_path)
    for src, by_did in docs.items():
        cols = store.id_columns(src)
        for did, key in by_did.items():
            assert set(key) == set(cols), (src, did)
            where = " AND ".join(f"{c} = ?" for c in cols)
            row = conn.execute(
                f"SELECT COUNT(*) FROM {store.table(src)} WHERE {where}",
                [key[c] for c in cols],
            ).fetchone()
            assert row[0] == 1, (src, did, key)

    # A stated id is served verbatim; the map must agree.
    assert docs["github"]["gh-stated"] == {"repo": "acme/app", "number": 7}
    assert docs["s3"]["o1"] == {"bucket": "eng", "key": "docs/readme.md"}


def test_id_map_containers_carry_the_ids_the_routers_serve(tmp_path):
    settings, manifest = _load_with_map(tmp_path, _ID_MAP_CORPUS)
    containers = manifest["containers"]

    assert containers["slack"]["general"] == {"id": synth.slack_channel_id("general")}
    assert containers["google_drive"]["Design"] == {"id": synth.drive_folder_id("Design")}
    assert containers["confluence"]["ENG"] == {"key": synth.confluence_space_key("ENG")}

    conn = sqlite3.connect(settings.db_path)
    team, sid, skey = conn.execute(
        "SELECT team, served_id, served_key FROM linear_teams"
    ).fetchone()
    assert containers["linear"][team] == {"id": sid, "key": skey}
    project, key = conn.execute("SELECT project, key FROM jira_projects").fetchone()
    assert containers["jira"][project] == {"key": key}
    # The jira key in `documents` carries the project's own prefix.
    issue_key = manifest["documents"]["jira"]["j1"]["key"]
    assert issue_key.startswith(f"{key}-")


def test_an_appended_id_map_emits_only_the_run_it_belongs_to(tmp_path):
    settings, first = _load_with_map(tmp_path, _ID_MAP_CORPUS, name="one.jsonl")
    more = [
        {
            "source_type": "slack",
            "doc_id": "s2",
            "channel": "random",
            "content": "later",
            "author_email": "a@x.com",
            "created": "2024-01-02T03:05:06Z",
        },
    ]
    _, second = _load_with_map(tmp_path, more, name="two.jsonl", reset=False, settings=settings)

    assert list(second["documents"]) == ["slack"]
    assert list(second["documents"]["slack"]) == ["s2"]
    # Containers are the DB's current state: the first run's channel is still addressable.
    assert set(second["containers"]["slack"]) == {"general", "random"}


def test_a_dry_run_refuses_the_id_map(tmp_path):
    """The CLI refuses this as a parameter conflict (see test_cli.py); this is the same refusal for
    the library entry point, which no `typer` context reaches."""
    corpus = _write(tmp_path, _ID_MAP_CORPUS)
    with pytest.raises(SystemExit, match="--dry-run assigns none"):
        byo.run(corpus, dry_run=True, id_map=tmp_path / "m.json")


@pytest.mark.parametrize("reset", [True, False], ids=["replace", "append"])
def test_an_unwritable_id_map_is_refused_before_the_load_starts(tmp_path, reset):
    """The manifest is written after the load commits, so a destination that cannot be written has
    to be caught up front. Otherwise a replace rolls the DB back to the corpus its freshly written
    tokens.yaml no longer describes, and an --append (which has no salvage) keeps the rows it added
    while the command dies, so a re-run appends them twice."""
    settings = Settings(data_dir=tmp_path)
    load(_write(tmp_path, _ID_MAP_CORPUS, "one.jsonl"), settings)
    before = _dump_tables(settings.db_path)
    tokens_before = settings.tokens_path.read_text()

    more = [
        {
            "source_type": "jira",
            "doc_id": "j2",
            "project": "payments",
            "title": "later",
            "content": "x",
            "author_email": "someone-else@y.com",
        }
    ]
    with pytest.raises(SystemExit, match="--id-map"):
        load(
            _write(tmp_path, more, "two.jsonl"),
            settings,
            reset=reset,
            id_map=tmp_path / "no" / "such" / "ids.json",
        )

    # Nothing moved: not the rows, and not the tokens that describe whose rows they are.
    assert _dump_tables(settings.db_path) == before
    assert settings.tokens_path.read_text() == tokens_before


def test_a_refused_load_leaves_no_id_map_behind(tmp_path):
    """The up-front check opens the destination for append, which is the only way to learn that an
    existing file cannot be written — and which creates one when there was none. A load that fails
    after it must not leave that 0-byte manifest sitting there: it is the empty file the --dry-run
    refusal exists to prevent, and tooling reading it would join through nothing."""
    settings = Settings(data_dir=tmp_path)
    dest = tmp_path / "ids.json"
    # As written: `complete` has no values for a source type that does not exist, which is the
    # refusal under test.
    bad = _ID_MAP_CORPUS + [{"source_type": "nope", "title": "t", "content": "c"}]

    with pytest.raises(SystemExit, match="source_type must be one of"):
        load(_write(tmp_path, bad, raw=True), settings, id_map=dest)
    assert not dest.exists()

    # One that was already there is not this check's to delete, so it keeps its contents until a
    # load actually succeeds and overwrites them.
    dest.write_text("stale\n")
    with pytest.raises(SystemExit, match="source_type must be one of"):
        load(_write(tmp_path, bad, "again.jsonl", raw=True), settings, id_map=dest)
    assert dest.read_text() == "stale\n"

    load(_write(tmp_path, _ID_MAP_CORPUS, "good.jsonl"), settings, id_map=dest)
    assert json.loads(dest.read_text())["format"] == "backlot-id-map/1"


def test_an_id_map_that_fails_at_the_last_moment_keeps_the_import(tmp_path, monkeypatch):
    """The write is outside the salvage-protected region, so a destination that passes the up-front
    check and breaks anyway costs the manifest, not the corpus."""
    settings = Settings(data_dir=tmp_path)
    dest_dir = tmp_path / "vanishes"
    dest_dir.mkdir()
    dest = dest_dir / "ids.json"

    real = byo._id_map_manifest

    def and_then_the_directory_goes_away(loader, conn):
        text = real(loader, conn)
        # The check does not leave a file here, so the directory is already empty.
        dest_dir.rmdir()
        return text

    monkeypatch.setattr(byo, "_id_map_manifest", and_then_the_directory_goes_away)
    with pytest.raises(OSError):
        load(_write(tmp_path, _ID_MAP_CORPUS), settings, id_map=dest)

    conn = store.connect_ro(settings.db_path)
    assert conn.execute("SELECT COUNT(*) FROM jira_issues").fetchone()[0] == 1
    assert settings.tokens_path.exists()


def test_one_dataset_id_on_two_rows_is_refused(tmp_path):
    """Where the corpus STATES the id, two records sharing a `doc_id` leave two rows, and nothing
    says which of them a link naming that `doc_id` meant — `resolve_cross_references` reads the same
    per-doc_id dict and would bind to whichever was written last. Refused at the second row."""
    records = [
        {
            "source_type": "jira",
            "doc_id": "dup",
            "project": "payments",
            "key": "PAY-1",
            "title": "one",
            "content": "x",
            "author_email": "a@x.com",
        },
        {
            "source_type": "jira",
            "doc_id": "dup",
            "project": "payments",
            "key": "PAY-2",
            "title": "two",
            "content": "x",
            "author_email": "a@x.com",
        },
    ]
    with pytest.raises(SystemExit, match="both call themselves 'dup'"):
        load(_write(tmp_path, records), Settings(data_dir=tmp_path))


def test_two_records_that_settle_on_one_row_still_share_a_dataset_id(tmp_path):
    """The counterpart: the guard is "one dataset id, two ROWS", not "a `doc_id` repeats". A real
    corpus has pairs that state no key and no number, and those are meant to collapse — a keyless
    jira issue settles on the key the first one took, confluence reuses its memo, slack its ts."""
    records = [
        {
            "source_type": "jira",
            "doc_id": "same",
            "project": "payments",
            "title": "one",
            "content": "x",
            "author_email": "a@x.com",
        },
        {
            "source_type": "jira",
            "doc_id": "same",
            "project": "payments",
            "title": "two",
            "content": "x",
            "author_email": "a@x.com",
        },
    ]
    settings = Settings(data_dir=tmp_path)
    out = tmp_path / "ids.json"
    load(_write(tmp_path, records), settings, id_map=out)

    conn = store.connect_ro(settings.db_path)
    # One row, the later record's (DO UPDATE), and one manifest entry pointing at it.
    rows = conn.execute("SELECT title, key FROM jira_issues").fetchall()
    assert [r["title"] for r in rows] == ["two"]
    key = rows[0]["key"]
    assert json.loads(out.read_text())["documents"]["jira"] == {"same": {"key": key}}
