#!/usr/bin/env python3
"""Import EnterpriseRAG-Bench, then serve it — a self-contained walkthrough.

Runs the one-command import into examples/import-enterpriserag-bench/data (downloading on the
first run; cached afterwards), starts a real mock server against it, prints what got served, and
keeps serving until you press Ctrl+C:

    python examples/import-enterpriserag-bench/run.py

The import takes no options — it imports the corpus, whole — so this takes none either.
"""

import json
import os
import socket
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
DATA = Path(__file__).resolve().parent / "data"


def _free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


env = {**os.environ, "BACKLOT_DATA_DIR": str(DATA)}

# 1. import the bench (download -> load -> ACL) into examples/import-enterpriserag-bench/data
# `-m backlot` so the CLI runs under THIS interpreter (no activated venv needed on PATH); it is the
# same `backlot import --type enterpriserag-bench` you would run by hand.
subprocess.run(
    [sys.executable, "-m", "backlot", "import", "--type", "enterpriserag-bench"],
    cwd=ROOT,
    env=env,
    check=True,
)

# 2. serve it and read it back over HTTP
port = _free_port()
proc = subprocess.Popen(
    [
        sys.executable,
        "-m",
        "backlot",
        "serve",
        "--port",
        str(port),
        "--log-level",
        "warning",
    ],
    cwd=ROOT,
    env=env,
)
base = f"http://127.0.0.1:{port}"
try:
    for _ in range(100):
        try:
            if urllib.request.urlopen(f"{base}/health", timeout=0.5).status == 200:
                break
        except Exception:  # noqa: BLE001
            time.sleep(0.1)
    health = json.loads(urllib.request.urlopen(f"{base}/health").read())
    print(f"\nserving {health['documents']} docs at {base}")
    print(f"  by source: {health['by_source']}")
    print("\nPress Ctrl+C to stop.")
    proc.wait()  # keep serving until the server exits or Ctrl+C
except KeyboardInterrupt:
    print("\nshutting down…")
finally:
    proc.terminate()
    try:
        proc.wait(timeout=10)
    except subprocess.TimeoutExpired:
        proc.kill()
