#!/usr/bin/env python3
"""Bring-Your-Own corpus, end to end: validate a JSONL, then serve it. Self-contained.

Validates the sample corpus against the schemas, starts a real server backed by it
(a uvicorn subprocess — nothing else needs to be running), prints what got served, and keeps
serving until you press Ctrl+C.

    python examples/bring-your-own-corpus/run.py

Swap CORPUS for your own JSONL to serve your own documents.
"""

import json
import subprocess
import sys
import time
from pathlib import Path

import httpx

from backlot import serve

CORPUS = Path(__file__).resolve().parent / "sample_corpus.jsonl"

# 1. Validate the corpus against backlot/schemas/ before serving anything (the same CLI you'd run by
# hand: `backlot import <corpus> --dry-run`). Spelled `-m backlot` so it runs under THIS interpreter
# and needs no activated venv on PATH; the package resolves from a checkout or site-packages alike.
if subprocess.run([sys.executable, "-m", "backlot", "import", str(CORPUS), "--dry-run"]).returncode:
    raise SystemExit("corpus is invalid")

# 2. Serve it with a real server and keep it running until Ctrl+C.
# split on "\n" only, not splitlines(): JSON Lines separates records by \n, and
# splitlines() also breaks on U+2028/U+2029 — ordinary characters inside a JSON string.
records = [json.loads(line) for line in CORPUS.read_text().split("\n") if line.strip()]
with serve(records) as s:
    health = httpx.get(f"{s.base_url}/health").json()
    print(f"\nserving {health['documents']} docs at {s.base_url}")
    print(f"  by source: {health['by_source']}")
    print("\nPress Ctrl+C to stop.")
    try:
        while True:
            time.sleep(3600)
    except KeyboardInterrupt:
        print("\nshutting down…")
