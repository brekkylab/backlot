#!/usr/bin/env python3
"""Read Backlot's Google Drive through fsspec — and a Google Sheet straight into a DataFrame.

``gdrive_fsspec`` is the fsspec implementation registered for ``gdrive://``. Unlike s3fs it has no
endpoint argument: it builds its Drive service with a bare ``build("drive", "v3")``, which resolves
to www.googleapis.com. ``backlot.integrations.fsspec.drive_filesystem_at`` supplies the endpoint,
and works around three ``gdrive_fsspec`` bugs that have nothing to do with Backlot — they
reproduce against real Google Drive too. Each one is named at its override in that module.

    pip install -e ".[fsspec]"
    python examples/using-fsspec/gdrive.py
    python examples/using-fsspec/gdrive.py --url http://localhost:8000 --token <usr-token>
"""

import argparse

import pandas as pd

from backlot import serve_or_connect
from backlot.integrations.fsspec import drive_filesystem_at

_REVENUE = "region,quarter,revenue\nEMEA,{q},120\nAMER,{q},240\nAPAC,{q},90\n"


def _doc(folder, title, content, **kw):
    return {
        "source_type": "google_drive",
        "folder": folder,
        "title": title,
        "content": content,
        "author_email": "ava@acme.com",
        "created": "2026-01-02T09:00:00Z",
        "updated": "2026-01-03T09:00:00Z",
        **kw,
    }


CORPUS = [
    # Left as the default `document` subtype: a Google-native Doc, which has no binary content.
    _doc("Handbook", "Engineering Onboarding", "Badge, laptop, first PR by Friday."),
    # An ordinary uploaded file — the one kind gdrive_fsspec can already download by itself.
    _doc("Handbook", "broker-notes.txt", "Raised the prefetch window to 512.", subtype="txt"),
    # A native Sheet. Drive exports these as CSV, which is what makes the pandas read below work.
    _doc("Finance", "Q1 Revenue", _REVENUE.format(q="Q1"), subtype="spreadsheet"),
    _doc("Finance", "Q2 Revenue", _REVENUE.format(q="Q2"), subtype="spreadsheet"),
]


_NATIVE = "application/vnd.google-apps."


def main(fs) -> None:
    print("=== fs.ls('') — My Drive ===")
    for folder in fs.ls("", detail=True):
        print(f"  {folder['name']}/  [{folder['type']}]")

    # mimeType rather than size: a listing carries Drive's own size, which for a native file is
    # the stored content, not the longer export a read returns. `fs.info(path)["size"]` is the
    # reconciled one. The mimeType is what decides which of the two reads below applies.
    print("\n=== walk the tree ===")
    files = [e for folder in fs.ls("", detail=False) for e in fs.ls(folder, detail=True)]
    for entry in files:
        print(f"  {entry['name']:38} {entry['mimeType']}")

    # Discovered by mimeType, not named: with --url this runs against that server's corpus.
    doc = next((e for e in files if e["mimeType"] == _NATIVE + "document"), None)
    sheets = [e for e in files if e["mimeType"] == _NATIVE + "spreadsheet"]

    # A Google Doc stores no bytes to download: `alt=media` answers 403 fileNotDownloadable, here
    # and on real Drive alike. The filesystem falls back to files.export, so a read just works.
    if doc:
        print(f"\n=== cat {doc['name']!r} — a native Doc, served through files.export ===")
        body = fs.cat_file(doc["name"]).decode()
        print("  " + body.replace("\n", "\n  ").rstrip())

    # The same export path, pointed at a Sheet: Drive hands back CSV, pandas parses it. Nothing
    # here knows it is talking to Backlot rather than to Google.
    for sheet in sheets:
        try:
            df = pd.read_csv(fs.open(sheet["name"]))
        except pd.errors.ParserError:
            continue  # a Sheet whose cells are prose, not a table — try the next one
        print(f"\n=== {sheet['name']!r} as a DataFrame ({df.shape[0]}x{df.shape[1]}) ===")
        print("  " + df.head().to_string(index=False).replace("\n", "\n  "))
        break
    else:
        print("\n(no Sheet in this corpus parses as CSV — the export above is the point)")


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Read Backlot's Drive through fsspec and pandas.")
    p.add_argument("--url", help="Backlot base URL (default: spin up a local throwaway server)")
    p.add_argument("--token", help="bearer token to read as (default: Backlot's admin token)")
    return p.parse_args()


if __name__ == "__main__":
    args = _parse_args()
    with serve_or_connect(CORPUS, url=args.url) as s:
        main(drive_filesystem_at(s.base_url, args.token or s.token))
