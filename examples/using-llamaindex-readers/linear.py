#!/usr/bin/env python3
"""Load Linear issues through the official llama-index Linear reader. Self-contained.

`LinearReader` hardcodes `graphql_endpoint = "https://api.linear.app/graphql"` as a **local
variable inside `load_data`** — no constructor argument, no module-level constant — so the
rebind-a-URL-constant trick that `patch_notion_at` uses has nothing to rebind. The one seam left
is the reader module's `import requests`, which `patch_linear_at()` swaps for a proxy that
rewrites Linear's host and forwards everything else untouched.

The query is caller-supplied (`load_data(query)`), so it lives here — which makes this script
double as a readable statement of what the mock's schema supports.

    pip install -e ".[examples,llamaindex]"
    pip install llama-index-readers-linear
    python examples/using-llamaindex-readers/linear.py            # or: --url http://localhost:8000
    python examples/using-llamaindex-readers/linear.py --url http://localhost:8000 --token <usr-token>

ONE CLIENT-SIDE BUG TO KNOW ABOUT. The reader does `issue.get("assignee", {}).get("name", "")`.
A GraphQL response always includes a selected field, so an *unassigned* issue comes back as
`assignee: null` — present, not absent — `.get`'s default never applies, and the reader raises
`AttributeError: 'NoneType' object has no attribute 'get'`. The same holds for `project`, `state`
and `creator`. Real Linear returns null for those too, so this reproduces against
api.linear.app and no mock-side change can fix it. The query below therefore filters to issues
that have both an assignee and a project — server-side, which the mock compiles into SQL.
"""

import argparse

from llama_index.readers.linear import LinearReader

from backlot import serve_or_connect
from backlot.integrations.llamaindex import linear_base_url, patch_linear_at

CORPUS = [
    {
        "source_type": "linear",
        "doc_id": "lin-kv",
        "team": "engineering",
        "title": "Variant-aware GPU allocation and KV residency",
        "content": "Long-context configs push peak GPU memory into fragile regions.",
        "author_email": "amaya.chen@acme.com",
        "identifier": "ENG-49121",
        "state": "In Progress",
        "priority": "P1",
        "estimate": 5,
        "labels": ["kv-cache", "memory"],
        "project": "runtime-memory-2025",
        "cycle": "2025-W08",
        "dueDate": "2025-03-15",
        "assignee": "diego.martinez@acme.com",
        "assigneeName": "Diego Martinez",
        "comments": [
            {
                "content": "Residency bands cut peak memory 20%.",
                "author_email": "diego.martinez@acme.com",
            }
        ],
    },
    {
        "source_type": "linear",
        "doc_id": "lin-batch",
        "team": "engineering",
        "title": "Continuous batching stalls after compaction",
        "content": "A 50ms stall when the batcher merges requests right after compaction.",
        "author_email": "diego.martinez@acme.com",
        "identifier": "ENG-49188",
        "state": "Done",
        "priority": "P0",
        "estimate": 3,
        "labels": ["latency"],
        "project": "runtime-memory-2025",
        "assignee": "amaya.chen@acme.com",
        "assigneeName": "Amaya Chen",
    },
    # Unassigned on purpose: it is what the `assignee: {null: false}` filter below exists for.
    {
        "source_type": "linear",
        "doc_id": "lin-triage",
        "team": "engineering",
        "title": "Triage: intermittent 502s on the gateway",
        "content": "Reported twice this week; no owner yet.",
        "author_email": "amaya.chen@acme.com",
        "identifier": "ENG-49200",
        "state": "Triage",
    },
]

# The reader's own field set, plus the filter that keeps its null-dereference bug out of the way.
# `load_data(query)` takes a document and nothing else — there is no variables argument — so the
# team is formatted in rather than passed as `$id`. "ENG" is the team KEY; the mock also accepts
# the team's UUID or its name.
QUERY = """
query Team {
  team(id: "%s") {
    issues(filter: {assignee: {null: false}, project: {null: false}}, first: 50) {
      nodes {
        id
        title
        description
        createdAt
        updatedAt
        archivedAt
        autoArchivedAt
        autoClosedAt
        branchName
        canceledAt
        completedAt
        dueDate
        estimate
        creator { name }
        assignee { name }
        state { name }
        project { name }
        labels { nodes { name } }
      }
    }
  }
}
"""


def build(mock, token):
    patch_linear_at(linear_base_url(mock.base_url))
    return LinearReader(api_key=token)


def main(reader, team="ENG"):
    docs = reader.load_data(QUERY % team)
    if not docs:
        # The reader swallows a GraphQL error envelope and returns [], so say so rather than
        # printing a cheerful "loaded 0".
        raise SystemExit(
            f"no issues came back for team {team!r} — the reader discards GraphQL "
            f"errors silently, so check the query against the mock's schema."
        )
    print(f"loaded {len(docs)} Document(s):")
    for d in docs:
        m = d.metadata  # `extra_info` is the deprecated alias for this
        print(f"  - {m['title']}")
        print(f"      state={m['state']}  assignee={m['assignee']}  project={m['project']}")
        print(f"      estimate={m['estimate']}  due={m['due_date']}  labels={m['labels']}")
        print(f"      branch={m['branch_name']}")
        print(f"      text: {d.text[:70]!r}")


def _parse_args():
    p = argparse.ArgumentParser(description="Load Linear issues via llama-index against the mock.")
    p.add_argument("--url", help="mock base URL (default: spin up a local throwaway mock)")
    p.add_argument("--token", help="mock bearer token from GET /_mock/users (default: admin)")
    return p.parse_args()


if __name__ == "__main__":
    args = _parse_args()
    with serve_or_connect(CORPUS, url=args.url) as mock:
        if args.token:
            print("authenticating with --token → responses are ACL-filtered to that user")
        main(build(mock, args.token or mock.token))
