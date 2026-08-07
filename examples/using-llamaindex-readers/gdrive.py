#!/usr/bin/env python3
"""Load Google Drive files through the official llama-index Google reader. Self-contained.

GoogleDriveReader builds its Drive service with no host override; point_drive_at() wraps the
`googleapiclient.discovery.build` symbol it locally imports on every call to target the mock (same
shim as gmail.py, with Drive's `/drive/v3` service path added to the endpoint). Auth is an ordinary
Google service-account credential from the mock, exactly as against real Drive.

Credential injection: `GoogleDriveReader.__init__` accepts `service_account_key` (a raw dict) and
`_get_credentials()` turns it into a Credentials object itself — but it does so with
`from_service_account_info(self.service_account_key, scopes=SCOPES)`, dropping any `subject`, so
that path alone can't do domain-wide-delegation impersonation (there's no way to hand it a
`subject` through the dict). For the bare-admin case (no `--user`) we use that hook directly:
`GoogleDriveReader(service_account_key=sa_info)`, no monkeypatching at all. For the impersonation
case (`--user <email>`) we build the Credentials object ourselves with `subject=...` (exactly as
`using-official-sdk/gdrive.py` does) and hand it back by overriding `reader._get_credentials` on
this *instance* only (not the class) — `load_data()` unconditionally calls
`self._creds = self._get_credentials()` at the top, so setting `reader._creds` directly wouldn't
survive. Scoped to the instance, this needs no try/finally: it dies with the reader object and
never leaks into another reader or another test, unlike a class-level patch.

    pip install -e ".[examples,llamaindex]"
    python examples/using-llamaindex-readers/gdrive.py                    # bare SA → admin
    python examples/using-llamaindex-readers/gdrive.py --url http://localhost:8000 --user mia@acme.com
"""

import argparse
import sys
from pathlib import Path

from google.oauth2 import service_account
from llama_index.readers.google import GoogleDriveReader

from backlot import serve_or_connect
from backlot.integrations.llamaindex import point_drive_at

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


def build(mock, user):
    point_drive_at(mock.base_url)
    sa_info, subject = google_service_account_info(mock.base_url, user)
    reader = GoogleDriveReader(service_account_key=sa_info)
    if subject:
        # service_account_key alone can't carry `subject` through the reader's own
        # `_get_credentials()` — build the delegated credential ourselves and hand it back via an
        # instance-only override (see module docstring).
        creds = service_account.Credentials.from_service_account_info(
            sa_info, scopes=["https://www.googleapis.com/auth/drive.readonly"], subject=subject
        )
        reader._get_credentials = lambda: creds
    return reader


def main(reader):
    docs = reader.load_data(folder_id="root")  # walk the whole (visible) tree from My Drive root
    print(f"loaded {len(docs)} Document(s):")
    for d in docs:
        print(f"  - {d.metadata.get('file path', d.doc_id)}: {d.text.splitlines()[-1][:60]}")


def _parse_args():
    p = argparse.ArgumentParser(description="Load Google Drive via llama-index against the mock.")
    p.add_argument("--url", help="mock base URL (default: spin up a local throwaway mock)")
    p.add_argument("--user", help="email to impersonate via the service account (default: admin)")
    return p.parse_args()


if __name__ == "__main__":
    args = _parse_args()
    with serve_or_connect(CORPUS, url=args.url) as mock:
        main(build(mock, args.user))
