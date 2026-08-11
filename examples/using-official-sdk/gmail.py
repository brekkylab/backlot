#!/usr/bin/env python3
"""Read Gmail through the official google-api-python-client — authenticating with an OAuth
**authorized-user** credential (client_id/secret + a refresh token), the classic 3-legged flow
a Gmail connector uses. Self-contained: run it directly. (For the service-account flow, see
gdrive.py.)

    pip install -e ".[examples]"
    python examples/using-official-sdk/gmail.py                        # first user (ceo, locally)
    python examples/using-official-sdk/gmail.py --user ceo@acme.com    # a specific user (ACL)
    python examples/using-official-sdk/gmail.py --url http://localhost:8000 --user <email>
"""

import argparse
import sys
from pathlib import Path

from google.api_core.client_options import ClientOptions
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build

from backlot import serve_or_connect

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

_p = argparse.ArgumentParser(
    description="Read Gmail through google-api-python-client against the mock."
)
_p.add_argument("--url", help="mock base URL to drive (default: spin up a local throwaway mock)")
_p.add_argument(
    "--user",
    help="which user's OAuth token to use, from GET /_mock/users (default: the first user)",
)
args = _p.parse_args()

with serve_or_connect(CORPUS, url=args.url) as mock:
    # An ordinary Google authorized-user credential — exactly as against real Gmail; only the
    # api_endpoint changes. The mock provides the client_id/secret + refresh token, and the
    # library refreshes against token_uri (the mock's /oauth2/token) to get an access token.
    client_id, client_secret, refresh_token, token_uri = google_oauth_user(mock.base_url, args.user)
    creds = Credentials(
        None,
        refresh_token=refresh_token,
        token_uri=token_uri,
        client_id=client_id,
        client_secret=client_secret,
    )
    gmail = build(
        "gmail",
        "v1",
        credentials=creds,
        static_discovery=True,
        client_options=ClientOptions(api_endpoint=mock.base_url),
    )

    ids = gmail.users().messages().list(userId="me", maxResults=5).execute().get("messages", [])
    if not ids:
        print("no messages visible to this identity")
    else:
        msg = gmail.users().messages().get(userId="me", id=ids[0]["id"], format="full").execute()
        headers = {h["name"]: h["value"] for h in msg["payload"]["headers"]}
        print(f"{len(ids)} messages; first message:")
        print(f"  Subject: {headers['Subject']}")
        print(f"  From:    {headers.get('From')}")
        print(f"  Snippet: {msg['snippet']}")
