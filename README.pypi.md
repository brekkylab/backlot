# Backlot

[![tests](https://github.com/brekkylab/backlot/actions/workflows/ci.yml/badge.svg)](https://github.com/brekkylab/backlot/actions/workflows/ci.yml)
[![python](https://img.shields.io/pypi/pyversions/backlot)](https://pypi.org/project/backlot/)
[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](https://github.com/brekkylab/backlot/blob/main/LICENSE)
[![Discord](https://img.shields.io/badge/Discord-5865F2?logo=discord&logoColor=white)](https://discord.gg/XCSsxYH6R)
[![X](https://img.shields.io/badge/Tweet-000000?logo=x&logoColor=white)](https://x.com/brekkylab)

**Bring your own enterprise. Serve it like the real thing.**

Backlot serves enterprise SaaS APIs — Slack, Gmail, Drive, GitHub, Jira, Notion, S3, and
more — with the exact response shapes, pagination, auth and per-document ACLs the real ones have,
over a corpus you supply. **No real account. No token issuance. No external network.**

```bash
pip install backlot
backlot import --bundled    # the bundled corpus -> data/mock.sqlite + data/tokens.yaml
backlot serve               # http://127.0.0.1:8000
```

Or run one programmatically on a free port:

```python
import backlot
from slack_sdk import WebClient

with backlot.mock_server() as m:
    slack = WebClient(token=m.token, base_url=f"{m.base_url}/slack/api/")
    print(slack.conversations_list()["channels"])
```

The endpoint reference for every service, the corpus and auth docs, and runnable examples are on GitHub:
[github.com/brekkylab/backlot](https://github.com/brekkylab/backlot).

## Trademarks

Backlot is an independent project, **not affiliated with, endorsed by, or sponsored by** any of the
vendors whose APIs it imitates. Slack, Gmail, Google Drive, Google Docs, Google Sheets, Google
Slides, GitHub, Jira, Confluence, Notion, Amazon S3, HubSpot, Linear and Fireflies are trademarks of
their respective owners, named here only to identify the APIs Backlot serves a compatible subset of.
Backlot ships no vendor logo, wordmark or brand asset.

## License

[MIT](https://github.com/brekkylab/backlot/blob/main/LICENSE)
