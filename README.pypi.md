# Backlot

**Change the base URL. Nothing else.**

Backlot serves enterprise SaaS APIs — Slack, Gmail, Drive, GitHub, Jira, Notion, S3, and
more — with the exact response shapes, pagination, auth and per-document ACLs the real ones have,
over a corpus you supply.

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

[MIT](https://github.com/brekkylab/backlot/blob/main/LICENSE)
