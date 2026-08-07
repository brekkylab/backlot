#!/usr/bin/env python3
"""Read Notion through the official notion-client SDK. Self-contained: run it directly.

    pip install -e ".[examples]"
    python examples/using-official-sdk/notion.py            # or: --url http://localhost:8000
    python examples/using-official-sdk/notion.py --url http://localhost:8000 --token <usr-token>

The only change from talking to real Notion is ``base_url`` — point it at the mock's ``/notion``
prefix (the SDK appends ``/v1/`` itself). The mock defaults to the ``2025-09-03`` API version, so
a database exposes a *data source* you query for its rows.
"""

import argparse

from notion_client import Client

from backlot import serve_or_connect

CORPUS = [
    {
        "source_type": "notion",
        "teamspace": "engineering",
        "title": "On-call Runbook",
        "content": "# On-call\n\nCheck dashboards, roll back, page on-call.",
        "comments": [{"content": "add the rate-limiter rollback step"}],
    },
    {
        "source_type": "notion",
        "teamspace": "engineering",
        "subtype": "database",
        "title": "Eng Tasks",
        "content": "Team task tracker.",
        "properties": {"Status": {"type": "select"}, "Priority": {"type": "select"}},
    },
    {
        "source_type": "notion",
        "teamspace": "engineering",
        "title": "Fix gateway 502s",
        "content": "Investigate token-bucket refill.",
        "parent": "eng-tasks-db",
        "doc_id": "eng-task-1",
        "properties": {"Status": "In Progress", "Priority": "High"},
    },
]
# give the database a stable doc_id so the row above can parent to it
CORPUS[1]["doc_id"] = "eng-tasks-db"

_p = argparse.ArgumentParser(
    description="Read Notion through the official notion-client SDK against the mock."
)
_p.add_argument("--url", help="mock base URL to drive (default: spin up a local throwaway mock)")
_p.add_argument(
    "--token",
    help="mock bearer token from GET /_mock/users "
    "(default: the admin token, which sees everything)",
)
args = _p.parse_args()

with serve_or_connect(CORPUS, url=args.url) as mock:
    if args.token:
        print("authenticating with --token → responses are ACL-filtered to that user")
    notion = Client(auth=args.token or mock.token, base_url=f"{mock.base_url}/notion")

    results = notion.search(query="on-call")["results"]
    print(f"search 'on-call' → {len(results)} result(s)")
    for r in results:
        title_prop = r.get("properties", {}).get("title", {}).get("title") or r.get("title") or []
        title = title_prop[0]["plain_text"] if title_prop else "(untitled)"
        print(f"  - {r['object']}: {title}")

    page = next((r for r in results if r["object"] == "page"), None)
    if page:
        blocks = notion.blocks.children.list(page["id"])["results"]
        print(f"\npage body ({len(blocks)} blocks):")
        for b in blocks:
            rt = b[b["type"]].get("rich_text", [])
            print(f"  | {''.join(t['plain_text'] for t in rt)}")
        comments = notion.comments.list(block_id=page["id"])["results"]
        for c in comments:
            print(f"  💬 {''.join(t['plain_text'] for t in c['rich_text'])}")

    # list databases via a type-filtered search, then query one for its rows
    dbs = notion.search(filter={"property": "object", "value": "database"})["results"]
    db = dbs[0] if dbs else None
    if db:
        dsid = db["data_sources"][0]["id"]  # 2025-09-03: rows live under a data source
        rows = notion.data_sources.query(data_source_id=dsid)["results"]
        print(f"\ndatabase '{db['title'][0]['plain_text']}' → {len(rows)} row(s):")
        for row in rows:
            name = row["properties"]["title"]["title"][0]["plain_text"]
            status = row["properties"].get("Status", {}).get("select", {}).get("name", "-")
            print(f"  - {name} [{status}]")

    print(f"\nauthenticated as: {notion.users.me()['name']}")
