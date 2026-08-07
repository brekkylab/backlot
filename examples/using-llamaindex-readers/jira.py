#!/usr/bin/env python3
"""Load Jira issues through the official llama-index Jira reader. Self-contained.

pip install -e ".[examples,llamaindex]"
python examples/using-llamaindex-readers/jira.py            # or: --url http://localhost:8000
python examples/using-llamaindex-readers/jira.py --url http://localhost:8000 --token <usr-token>
"""

import argparse
import sys
from pathlib import Path

from backlot import serve_or_connect
from backlot.integrations.llamaindex import atlassian_base_url

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from _common.syspath import drop_self_from_syspath

# This file is named jira.py; drop its own dir so the reader's `from jira import JIRA` resolves to
# the real `jira` package rather than this script.
drop_self_from_syspath(__file__)

from llama_index.readers.jira import JiraReader  # noqa: E402

CORPUS = [
    {
        "source_type": "jira",
        "project": "payments",
        "title": "SEV2: checkout latency spike",
        "content": "p95 checkout latency jumped to 2.1s after the payments migration.",
        "status": "In Progress",
        "issuetype": "Incident",
        "priority": "High",
    },
    {
        "source_type": "jira",
        "project": "payments",
        "title": "Write the postmortem",
        "content": "Draft the postmortem and action items.",
        "status": "To Do",
    },
]


def build(mock, token):
    return JiraReader(PATauth={"server_url": atlassian_base_url(mock.base_url), "api_token": token})


def main(reader):
    docs = reader.load_data(query="project = payments")
    print(f"loaded {len(docs)} Document(s):")
    for d in docs:
        print(f"  - {d.metadata.get('key', d.doc_id)}: {d.text.splitlines()[0][:70]}")


def _parse_args():
    p = argparse.ArgumentParser(description="Load Jira issues via llama-index against the mock.")
    p.add_argument("--url", help="mock base URL (default: spin up a local throwaway mock)")
    p.add_argument("--token", help="mock bearer token from GET /_mock/users (default: admin)")
    return p.parse_args()


if __name__ == "__main__":
    args = _parse_args()
    with serve_or_connect(CORPUS, url=args.url) as mock:
        if args.token:
            print("authenticating with --token → responses are ACL-filtered to that user")
        main(build(mock, args.token or mock.token))
