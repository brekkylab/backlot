#!/usr/bin/env python3
"""Load a Google Sheets workbook through the official llama-index Google reader. Self-contained.

`GoogleSheetsReader` builds its Sheets service with `discovery.build("sheets", "v4", …)` and no
host override; `point_sheets_at()` wraps `googleapiclient.discovery.build` to target Backlot (the
same shim gmail.py and gdrive.py use, with Sheets' `/sheets` service path on the endpoint). The
reader then asks `spreadsheets.get` for each sheet's dimensions and reads every sheet as
`R1C1:R{rowCount}C{columnCount}`, which is the R1C1 request Backlot's `values.get` accepts.

Credential injection: the reader has no constructor hook — `_get_credentials()` runs a disk-based
OAuth flow, as `GmailReader`'s does — so the example builds the authorized-user credential from
Backlot's OAuth client and the user's bearer token (the same `google_oauth_user` glue gmail.py
uses) and hands it back by overriding `_get_credentials` on this *instance* only.

The workbook is resolved through Drive by its exact name, `name = '…'`, the way a client that has
a title and not an id finds it.

What comes back is the reader's, faithfully: `load_data` in the installed reader (0.8.0) writes each
sheet's title and then reads `R1C1:R{rowCount}C{columnCount}` with NO sheet title in the range, so
every block after the first repeats the first sheet's grid — against real Sheets as much as here,
since an unqualified range is the first sheet's on both. A single-sheet workbook would hide that.

    pip install -e ".[official-sdk,llamaindex]"
    python examples/using-llamaindex-readers/gsheets.py                    # first user in /_meta/users
    python examples/using-llamaindex-readers/gsheets.py --url http://localhost:8000 --user mia@acme.com
"""

import argparse
import json
import sys
import urllib.parse
import urllib.request
from pathlib import Path

from google.oauth2.credentials import Credentials
from llama_index.readers.google import GoogleSheetsReader

from backlot import serve_or_connect
from backlot.integrations.llamaindex import point_sheets_at

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from _common.google_creds import google_oauth_user

CORPUS = [
    {
        "updated": "2026-01-10T08:00:00Z",
        "created": "2026-01-10T08:00:00Z",
        "source_type": "google_drive",
        "folder": "finance",
        "title": "Q1 Revenue Model",
        "subtype": "spreadsheet",
        "author_email": "cfo@acme.com",
        # A grid stated as `sheets` carries no `content`: Drive derives that from the first sheet.
        "sheets": [
            {
                "title": "Revenue",
                "grid": [["month", "revenue"], ["Jan", 120000], ["Feb", 135000], ["Mar", 142000]],
            },
            {"title": "Notes", "grid": [["Q1 closes 2026-03-31"]]},
        ],
    },
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
]
WORKBOOK = "Q1 Revenue Model"


def build(s, user):
    point_sheets_at(s.base_url)
    client_id, client_secret, refresh_token, token_uri = google_oauth_user(s.base_url, user)
    creds = Credentials(
        None,
        refresh_token=refresh_token,
        token_uri=token_uri,
        client_id=client_id,
        client_secret=client_secret,
    )
    reader = GoogleSheetsReader()
    reader._get_credentials = lambda: creds  # instance-only; see the module docstring
    return reader, refresh_token


def spreadsheet_id(base_url: str, token: str, name: str) -> str:
    """The workbook's id, resolved through Drive by exact name — ACL-scoped to the caller."""
    q = f"mimeType = 'application/vnd.google-apps.spreadsheet' and name = '{name}'"
    query = urllib.parse.urlencode({"q": q, "fields": "files(id)"})
    req = urllib.request.Request(
        f"{base_url.rstrip('/')}/drive/v3/files?{query}",
        headers={"Authorization": f"Bearer {token}"},
    )
    with urllib.request.urlopen(req) as r:
        files = json.load(r)["files"]
    if not files:
        sys.exit(f"no spreadsheet named {name!r} is visible to this user")
    return files[0]["id"]


def main(reader, base_url: str, token: str):
    sheet = spreadsheet_id(base_url, token, WORKBOOK)
    docs = reader.load_data(spreadsheet_ids=[sheet])  # one Document per workbook
    print(f"loaded {len(docs)} Document(s):")
    for d in docs:
        # Each sheet's title, then a grid tab-separated, one line per row — the first sheet's grid
        # under every title (see the module docstring).
        for line in d.text.splitlines():
            print(f"  {line}")


def _parse_args():
    p = argparse.ArgumentParser(description="Load Google Sheets via llama-index against Backlot.")
    p.add_argument("--url", help="Backlot base URL (default: spin up a local throwaway server)")
    p.add_argument(
        "--user", help="email to read as, from GET /_meta/users (default: the first user listed)"
    )
    return p.parse_args()


if __name__ == "__main__":
    args = _parse_args()
    with serve_or_connect(CORPUS, url=args.url) as s:
        reader, token = build(s, args.user)
        main(reader, s.base_url, token)
