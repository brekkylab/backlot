# Backlot

Backlot serves enterprise SaaS APIs (Slack, Gmail, Google Drive, GitHub, Jira, Confluence, Notion and more) from one local process, over a corpus you supply, with per-document access controls. A connector, an agent or a RAG pipeline built on the vendors' official SDKs can be exercised against it with no vendor account and no network.

This plugin carries the `backlot` agent skill. The skill teaches a coding agent to write a small corpus, load it, start Backlot and point a real client at it. How to use Backlot is in the skill itself.

## Install

```bash
claude plugin marketplace add brekkylab/backlot && claude plugin install backlot@brekkylab
codex plugin marketplace add brekkylab/backlot && codex plugin add backlot@brekkylab
```

## What the skill runs, sends and fetches

- **Installs the CLI.** When `backlot` is not on `PATH`, the skill installs it from PyPI with `pip install backlot`.
- **Starts a local server.** `backlot serve` listens on `127.0.0.1:8000` and answers only the corpus loaded into it.
- **Sends usage counts.** A running server sends anonymous counts of the vendor operations it is asked for to `telemetry.brekkylab.com`. `backlot serve --disable-telemetry` or `BACKLOT_TELEMETRY=false` turns this off; [docs/telemetry.md](https://github.com/brekkylab/backlot/blob/main/docs/telemetry.md) lists what is sent.
- **Downloads a public dataset only when asked.** `backlot import --type enterpriserag-bench` fetches that benchmark's documents from GitHub. The default corpus ships inside the package and downloads nothing.

## Links

- Source and documentation: [github.com/brekkylab/backlot](https://github.com/brekkylab/backlot)
- Package: [pypi.org/project/backlot](https://pypi.org/project/backlot/)
- License: [Apache-2.0](https://github.com/brekkylab/backlot/blob/main/LICENSE)
