"""--id-map: the manifest joining a corpus's own dataset ids to the ids the DB serves."""

import json
import sqlite3

import pytest

from backlot import store, synth
from backlot.config import Settings
from backlot.importer import byo
from backlot.importer.byo import load


def _write(tmp_path, records, name="corpus.jsonl"):
    p = tmp_path / name
    p.write_text("\n".join(json.dumps(r) for r in records))
    return p


CORPUS = [
    {
        "source_type": "github",
        "doc_id": "gh-stated",
        "repo": "acme/app",
        "number": 7,
        "title": "stated",
        "content": "a",
        "author_email": "a@x.com",
    },
    {
        "source_type": "github",
        "doc_id": "gh-keyless",
        "repo": "acme/app",
        "title": "keyless",
        "content": "b",
        "author_email": "a@x.com",
    },
    {
        "source_type": "jira",
        "doc_id": "j1",
        "project": "payments",
        "title": "t",
        "content": "c",
        "author_email": "a@x.com",
    },
    {
        "source_type": "slack",
        "doc_id": "s1",
        "channel": "general",
        "title": "t",
        "content": "hello",
        "author_email": "a@x.com",
        "created": "2024-01-02T03:04:05Z",
    },
    {
        "source_type": "s3",
        "doc_id": "o1",
        "bucket": "eng",
        "key": "docs/readme.md",
        "title": "readme",
        "content": "d",
        "author_email": "a@x.com",
    },
    {
        "source_type": "linear",
        "doc_id": "l1",
        "team": "engineering",
        "title": "t",
        "content": "e",
        "author_email": "a@x.com",
    },
    {
        "source_type": "google_drive",
        "doc_id": "d1",
        "folder": "Design",
        "title": "spec",
        "content": "f",
        "author_email": "a@x.com",
    },
    {
        "source_type": "confluence",
        "doc_id": "c1",
        "space": "ENG",
        "title": "page",
        "content": "g",
        "author_email": "a@x.com",
    },
    # doc_id omitted on purpose: the manifest must key it by the DEFAULTED dataset id.
    {
        "source_type": "gmail",
        "mailbox": "a@x.com",
        "title": "mail",
        "content": "h",
        "author_email": "a@x.com",
    },
]


def _load_with_map(tmp_path, records, name="c.jsonl", reset=True, settings=None):
    settings = settings or Settings(data_dir=tmp_path)
    out = tmp_path / f"{name}.idmap.json"
    load(_write(tmp_path, records, name), settings, reset=reset, id_map=out)
    return settings, json.loads(out.read_text())


def test_every_document_lands_in_the_map_under_its_served_key(tmp_path):
    settings, manifest = _load_with_map(tmp_path, CORPUS)
    docs = manifest["documents"]

    # One entry per record, keyed by the dataset id — including the defaulted one.
    assert sum(len(v) for v in docs.values()) == len(CORPUS)
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


def test_containers_carry_the_ids_the_routers_serve(tmp_path):
    settings, manifest = _load_with_map(tmp_path, CORPUS)
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


def test_append_emits_only_the_run_it_belongs_to(tmp_path):
    settings, first = _load_with_map(tmp_path, CORPUS, name="one.jsonl")
    more = [
        {
            "source_type": "slack",
            "doc_id": "s2",
            "channel": "random",
            "title": "t",
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


def test_dry_run_refuses_the_flag(tmp_path):
    corpus = _write(tmp_path, CORPUS)
    with pytest.raises(SystemExit, match="--dry-run assigns none"):
        byo.run(corpus, dry_run=True, id_map=tmp_path / "m.json")
