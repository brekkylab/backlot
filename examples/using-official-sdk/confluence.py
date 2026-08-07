#!/usr/bin/env python3
"""Read Confluence through the official atlassian-python-api. Self-contained.

    pip install -e ".[examples]"
    python examples/using-official-sdk/confluence.py            # or: --url http://localhost:8000
    python examples/using-official-sdk/confluence.py --url http://localhost:8000 \
        --username <email> --password <usr-token>   # ACL-filtered to that user
"""

import argparse

from atlassian import Confluence

from backlot import serve_or_connect

CORPUS = [
    {
        "source_type": "confluence",
        "space": "handbook",
        "title": "Engineering Handbook",
        "content": "How we build software: coding standards, review process, on-call.",
    },
    {
        "source_type": "confluence",
        "space": "handbook",
        "title": "On-call Runbook",
        "content": "Respond to gateway 502s: check dashboards, roll back, page on-call.",
    },
]

_p = argparse.ArgumentParser(
    description="Read Confluence through atlassian-python-api against the mock."
)
_p.add_argument("--url", help="mock base URL to drive (default: spin up a local throwaway mock)")
_p.add_argument(
    "--username",
    default="svc@example.com",
    help="Atlassian Basic-auth username (email); the mock resolves the caller by the token/password",
)
_p.add_argument(
    "--password",
    help="api token used as the Basic-auth password (default: --token, else the admin token)",
)
_p.add_argument("--token", help="alias for --password: a mock bearer token from GET /_mock/users")
args = _p.parse_args()

with serve_or_connect(CORPUS, url=args.url) as mock:
    username = args.username
    password = args.password or args.token or mock.token
    if args.username != "svc@example.com" or args.password or args.token:
        print(f"authenticating as {username} → responses are ACL-filtered to that user")
    confluence = Confluence(
        url=f"{mock.base_url}/atlassian/wiki", username=username, password=password
    )

    pages = confluence.get("rest/api/content", params={"limit": 5, "expand": "body.storage"})[
        "results"
    ]
    if not pages:
        print("no pages visible to this identity")
    else:
        page = confluence.get_page_by_id(pages[0]["id"], expand="body.storage")
        body = page["body"]["storage"]["value"]
        print(f"{len(pages)} pages; first page:")
        print(f"  title: {page['title']}")
        print(f"  body:  {body[:80]}")
