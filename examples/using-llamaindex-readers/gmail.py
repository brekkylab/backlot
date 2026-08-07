#!/usr/bin/env python3
"""Load Gmail messages through the official llama-index Google reader. Self-contained.

GmailReader builds its Google service with no host override; point_gmail_at() wraps the
`googleapiclient.discovery.build` symbol it locally imports on every call to target the mock.
Auth is an ordinary Google authorized-user credential (client_id/secret + refresh token) from the
mock, exactly as against real Gmail.

The installed GmailReader._get_credentials() unconditionally runs a local disk-based OAuth flow
(reads token.json / credentials.json off disk) every call, regardless of whether a `service` was
already supplied -- there's no constructor hook to inject credentials directly (this reader
version has no `credentials` field; setting one raises `ValueError`). We patch that method to
hand back the mock-issued credential instead of touching disk.

    pip install -e ".[examples,llamaindex]"
    python examples/using-llamaindex-readers/gmail.py                     # first user
    python examples/using-llamaindex-readers/gmail.py --url http://localhost:8000 --user ceo@acme.com
"""

import argparse
import sys
from pathlib import Path

from google.oauth2.credentials import Credentials
from llama_index.readers.google import GmailReader

from backlot import serve_or_connect
from backlot.integrations.llamaindex import point_gmail_at

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from _common.google_creds import google_oauth_user

CORPUS = [
    {
        "source_type": "gmail",
        "mailbox": "ceo",
        "title": "Q1 board deck draft",
        "content": "Draft narrative for the Q1 board meeting. Please review before Thursday.",
        "author_email": "ceo@acme.com",
    },
]


def build(mock, user):
    point_gmail_at(mock.base_url)
    client_id, client_secret, refresh_token, token_uri = google_oauth_user(mock.base_url, user)
    creds = Credentials(
        None,
        refresh_token=refresh_token,
        token_uri=token_uri,
        client_id=client_id,
        client_secret=client_secret,
    )
    import llama_index.readers.google.gmail.base as gm

    gm.GmailReader._get_credentials = lambda self: creds
    reader = GmailReader(
        query="", service=None, use_iterative_parser=True, max_results=10, results_per_page=None
    )
    return reader


def main(reader):
    docs = reader.load_data()
    print(f"loaded {len(docs)} Document(s):")
    for d in docs:
        print(f"  - {d.text.splitlines()[0][:80]}")


def _parse_args():
    p = argparse.ArgumentParser(description="Load Gmail via llama-index against the mock.")
    p.add_argument("--url", help="mock base URL (default: spin up a local throwaway mock)")
    p.add_argument("--user", help="which user's OAuth token to use (default: the first user)")
    return p.parse_args()


if __name__ == "__main__":
    args = _parse_args()
    with serve_or_connect(CORPUS, url=args.url) as mock:
        main(build(mock, args.user))
