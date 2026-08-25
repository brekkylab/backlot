# Backlot

[![Discord](https://img.shields.io/badge/Discord-join-5865F2?logo=discord&logoColor=white)](https://discord.gg/XCSsxYH6R)
[![X](https://img.shields.io/badge/X-%40brekkylab-000000?logo=x&logoColor=white)](https://x.com/brekkylab)

**Bring your own enterprise. Serve it like the real thing.**

Backlot serves enterprise SaaS APIs — Slack, Gmail, Drive, GitHub, Jira, Notion, S3, and
more — with the exact response shapes, pagination, auth and per-document ACLs the real ones have,
over a corpus you supply. Change the base URL; nothing else changes.

```bash
pip install backlot
backlot import --bundled    # the bundled corpus -> data/mock.sqlite + data/tokens.yaml
backlot serve               # http://127.0.0.1:8000
```

A corpus covering every source ships with the package, so there is nothing to fetch or write. Or
let a test spin one up on a free port:

```python
import backlot
from slack_sdk import WebClient

with backlot.mock_server() as m:
    slack = WebClient(token=m.token, base_url=f"{m.base_url}/slack/api/")
    print(slack.conversations_list()["channels"])
```

Every document carries its own readers, and each user's token sees only theirs — so "does this
leak?" is a test rather than an audit.

**The endpoint reference for every service, the corpus and auth docs, and runnable examples for
official SDKs, MCP servers, LlamaIndex readers and mirage are on GitHub:
[github.com/brekkylab/backlot](https://github.com/brekkylab/backlot).**

## Trademarks

Backlot is an independent project, **not affiliated with, endorsed by, or sponsored by** any of the
vendors whose APIs it imitates. Slack, Gmail, Google Drive, Google Docs, Google Sheets, Google
Slides, GitHub, Jira, Confluence, Notion, Amazon S3, HubSpot, Linear and Fireflies are trademarks of
their respective owners, named here only to identify the APIs Backlot serves a compatible subset of.
Backlot ships no vendor logo, wordmark or brand asset.

[MIT](https://github.com/brekkylab/backlot/blob/main/LICENSE)
