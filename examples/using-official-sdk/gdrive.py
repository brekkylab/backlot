#!/usr/bin/env python3
"""Read Google Drive through the official google-api-python-client — authenticating with a
Google **service-account** credential, the way a real connector does, rather than a raw token.
Self-contained.

    pip install -e ".[official-sdk]"
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
        "updated": "2025-11-01T12:00:00Z",
        "created": "2025-11-01T12:00:00Z",
        "source_type": "google_drive",
        "folder": "marketing",
        "title": "Brand guidelines v3",
        "content": "Logo usage, color palette, typography.",
        "subtype": "document",
        "author_email": "mia@acme.com",
    },
    {
        "updated": "2026-01-10T08:00:00Z",
        "created": "2026-01-10T08:00:00Z",
        "source_type": "google_drive",
        "folder": "finance",
        "title": "Q1 Revenue Model",
        # A real grid, so the Sheets API below can serve typed cells. Stating `sheets` means
        # stating no `content` — it is derived from the first sheet at import.
        "sheets": [
            {
                "title": "Monthly",
                "grid": [
                    ["month", "revenue", "cost", "profitable"],
                    ["Jan", 120000, 80000, True],
                    ["Feb", 135000, 82000, True],
                ],
            },
            {"title": "Assumptions", "grid": [["input", "value"], ["headcount", 42]]},
        ],
        "subtype": "spreadsheet",
        "author_email": "cfo@acme.com",
    },
    {
        "updated": "2026-01-25T15:00:00Z",
        "created": "2026-01-25T15:00:00Z",
        "source_type": "google_drive",
        "folder": "marketing",
        "title": "All-hands Q1 Deck",
        "content": "Slide 1: Welcome\n\nSlide 2: Roadmap",
        "subtype": "presentation",
        "author_email": "mia@acme.com",
    },
]

_p = argparse.ArgumentParser(
    description="Read Google Drive through google-api-python-client against Backlot."
)
_p.add_argument(
    "--url", help="Backlot base URL to drive (default: spin up a local throwaway server)"
)
_p.add_argument(
    "--user",
    help="email to impersonate via the service account (default: bare service account = admin, sees everything)",
)
args = _p.parse_args()

with serve_or_connect(CORPUS, url=args.url) as s:
    # Auth is an ordinary Google service-account credential; only the api_endpoint changes.
    # Backlot issues the key and honors the JWT exchange it triggers.
    sa_info, subject = google_service_account_info(
        s.base_url, args.user
    )  # stands in for the JSON key file
    creds = service_account.Credentials.from_service_account_info(
        sa_info, scopes=["https://www.googleapis.com/auth/drive.readonly"], subject=subject
    )
    gdrive = build(
        "drive",
        "v3",
        credentials=creds,
        static_discovery=True,
        client_options=ClientOptions(api_endpoint=f"{s.base_url}/drive/v3"),
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

    # The same credential drives Sheets v4. A spreadsheet whose corpus record states a grid serves
    # named sheets over typed cells, so UNFORMATTED_VALUE gives back 120000 and True rather than
    # "120000" and "TRUE".
    sheets = build(
        "sheets",
        "v4",
        credentials=creds,
        static_discovery=True,
        # No `/v4` here, unlike Drive above: the Sheets discovery document carries the version in
        # its own servicePath, so the client appends it to whatever endpoint it is given.
        client_options=ClientOptions(api_endpoint=f"{s.base_url}/sheets"),
    )
    book = next((f for f in files if f["name"] == "Q1 Revenue Model"), None)
    if book:
        meta = sheets.spreadsheets().get(spreadsheetId=book["id"]).execute()
        print(f"\n'{meta['properties']['title']}' has {len(meta['sheets'])} sheets:")
        for sh in meta["sheets"]:
            p = sh["properties"]
            print(f"  - [{p['index']}] {p['title']} (sheetId {p['sheetId']})")
        rows = (
            sheets.spreadsheets()
            .values()
            .get(
                spreadsheetId=book["id"],
                range="Monthly",
                valueRenderOption="UNFORMATTED_VALUE",
            )
            .execute()
            .get("values", [])
        )
        print("\nMonthly, unformatted:")
        for row in rows:
            print(f"  {row}")
