#!/usr/bin/env python3
"""Read Confluence through the official atlassian-python-api. Self-contained.

    pip install -e ".[examples]"
    python examples/using-official-sdk/confluence.py            # or: --url http://localhost:8000
    python examples/using-official-sdk/confluence.py --url http://localhost:8000 \
        --username <email> --password <usr-token>   # ACL-filtered to that user
"""

import argparse
import sys

from atlassian import Confluence

from backlot import serve_or_connect

CORPUS = [
    {
        "author_email": "ava@acme.com",
        "created": "2025-06-01T09:00:00Z",
        "source_type": "confluence",
        "space": "handbook",
        "title": "Engineering Handbook",
        "content": "How we build software: coding standards, review process, on-call.",
    },
    {
        "author_email": "ava@acme.com",
        "created": "2025-09-10T11:00:00Z",
        "source_type": "confluence",
        "space": "handbook",
        "title": "On-call Runbook",
        "content": "Respond to gateway 502s: check dashboards, roll back, page on-call.",
    },
]

_p = argparse.ArgumentParser(
    description="Read Confluence through atlassian-python-api against Backlot."
)
_p.add_argument(
    "--url", help="Backlot base URL to drive (default: spin up a local throwaway server)"
)
_p.add_argument(
    "--username",
    default="svc@example.com",
    help="Atlassian Basic-auth username: the address the token belongs to, which the real service requires to match (the placeholder works only for the admin token, which has none)",
)
_p.add_argument(
    "--password",
    help="api token used as the Basic-auth password (default: --token, else the admin token)",
)
_p.add_argument(
    "--token", help="alias for --password: a Backlot bearer token from GET /_meta/users"
)
args = _p.parse_args()

with serve_or_connect(CORPUS, url=args.url) as s:
    username = args.username
    password = args.password or args.token or s.token
    # Atlassian authenticates the PAIR: a user's api_token under someone else's address is a 401
    # on the real service, and Backlot answers the same. Both halves have to name one identity, so
    # either alone is refused rather than sent. The admin/service token is the exception both ways
    # — it has no address, so any username carries it, which is what `docs/auth.md` documents.
    named_token = (args.password or args.token) not in (None, s.token)
    named_user = args.username != "svc@example.com"
    if named_token and not named_user:
        sys.exit(
            "a --token/--password names one user, so pass --username with that user's own email "
            f"too (GET {s.base_url}/_meta/users lists both)"
        )
    if named_user and not named_token:
        sys.exit(
            f"--username alone would send the admin token under {args.username}, which Backlot "
            "takes as the admin: pass --token with that user's own token too "
            f"(GET {s.base_url}/_meta/users lists both)"
        )
    if named_user or named_token:
        print(f"authenticating as {username} → responses are ACL-filtered to that user")
    confluence = Confluence(
        url=f"{s.base_url}/atlassian/wiki", username=username, password=password
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
