#!/usr/bin/env python3
"""Read Google Drive through the official google-api-python-client — authenticating with a
Google **service-account** credential, the way a real connector does, rather than a raw token.
Self-contained.

    pip install -e ".[examples]"
    python examples/using-official-sdk/gdrive.py                          # bare SA → admin, sees all
    python examples/using-official-sdk/gdrive.py --user mia@acme.com      # impersonate a user (ACL)
    python examples/using-official-sdk/gdrive.py --url http://localhost:8000 --user <email>
"""

import argparse
import sys
from pathlib import Path

from google.api_core.client_options import ClientOptions
from google.oauth2 import service_account
from googleapiclient.discovery import build

from backlot import serve_or_connect

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from _common.google_creds import google_service_account_info

CORPUS = [
    {
        "source_type": "google_drive",
        "folder": "marketing",
        "title": "Brand guidelines v3",
        "content": "Logo usage, color palette, typography.",
        "subtype": "document",
        "author_email": "mia@acme.com",
    },
    {
        "source_type": "google_drive",
        "folder": "finance",
        "title": "Q1 Revenue Model",
        "content": "month,revenue\nJan,120000\nFeb,135000",
        "subtype": "spreadsheet",
        "author_email": "cfo@acme.com",
    },
    {
        "source_type": "google_drive",
        "folder": "marketing",
        "title": "All-hands Q1 Deck",
        "content": "Slide 1: Welcome\n\nSlide 2: Roadmap",
        "subtype": "presentation",
        "author_email": "mia@acme.com",
    },
]

_p = argparse.ArgumentParser(
    description="Read Google Drive through google-api-python-client against the mock."
)
_p.add_argument("--url", help="mock base URL to drive (default: spin up a local throwaway mock)")
_p.add_argument(
    "--user",
    help="email to impersonate via the service account (default: bare service account = admin, sees everything)",
)
args = _p.parse_args()

with serve_or_connect(CORPUS, url=args.url) as mock:
    # Auth is an ordinary Google service-account credential; only the api_endpoint changes. The
    # mock issues the key and honors the JWT exchange it triggers.
    sa_info, subject = google_service_account_info(
        mock.base_url, args.user
    )  # stands in for the JSON key file
    creds = service_account.Credentials.from_service_account_info(
        sa_info, scopes=["https://www.googleapis.com/auth/drive.readonly"], subject=subject
    )
    gdrive = build(
        "drive",
        "v3",
        credentials=creds,
        static_discovery=True,
        client_options=ClientOptions(api_endpoint=f"{mock.base_url}/drive/v3"),
    )

    files = gdrive.files().list(pageSize=5).execute()["files"]
    if not files:
        print("no files visible to this identity")
    else:
        body = gdrive.files().export(fileId=files[0]["id"], mimeType="text/plain").execute()
        text = body.decode() if isinstance(body, bytes) else body
        print(f"{len(files)} files:")
        for f in files:
            print(f"  - {f['name']}")
        print(f"\nExported '{files[0]['name']}' as text/plain ({len(text)} bytes):")
        print(f"  {text.splitlines()[0]}")
