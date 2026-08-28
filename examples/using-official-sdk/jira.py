#!/usr/bin/env python3
"""Read Jira through the official atlassian-python-api. Self-contained: run it directly.

    pip install -e ".[examples]"
    python examples/using-official-sdk/jira.py            # or: --url http://localhost:8000
    python examples/using-official-sdk/jira.py --url http://localhost:8000 \
        --username <email> --password <usr-token>   # ACL-filtered to that user
"""

import argparse

from atlassian import Jira

from backlot import serve_or_connect

CORPUS = [
    {
        "reporter": "bob@acme.com",
        "author_email": "bob@acme.com",
        "created": "2026-02-08T20:00:00Z",
        "source_type": "jira",
        "project": "payments",
        "title": "SEV2: checkout latency spike",
        "content": "p95 checkout latency jumped to 2.1s after the payments migration.",
        "status": "In Progress",
        "issuetype": "Incident",
        "priority": "High",
    },
    {
        "issuetype": "Task",
        "reporter": "ava@acme.com",
        "author_email": "ava@acme.com",
        "created": "2026-02-09T03:00:00Z",
        "source_type": "jira",
        "project": "payments",
        "title": "Write the postmortem",
        "content": "Draft the postmortem and action items.",
        "status": "To Do",
    },
]

_p = argparse.ArgumentParser(description="Read Jira through atlassian-python-api against Backlot.")
_p.add_argument(
    "--url", help="Backlot base URL to drive (default: spin up a local throwaway server)"
)
_p.add_argument(
    "--username",
    default="svc@example.com",
    help="Atlassian Basic-auth username (email); Backlot resolves the caller by the token/password",
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
    if args.username != "svc@example.com" or args.password or args.token:
        print(f"authenticating as {username} → responses are ACL-filtered to that user")
    jira = Jira(url=f"{s.base_url}/atlassian", username=username, password=password)

    issues = jira.get("rest/api/3/search/jql", params={"maxResults": 5})["issues"]
    if not issues:
        print("no issues visible to this identity")
    else:
        issue = jira.get(f"rest/api/3/issue/{issues[0]['key']}")
        print(f"{len(issues)} issues; first issue:")
        print(f"  {issue['key']}: {issue['fields']['summary']}")
        print(f"  status: {issue['fields']['status']['name']}")
