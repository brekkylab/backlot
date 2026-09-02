# Backlot

[![tests](https://github.com/brekkylab/backlot/actions/workflows/ci.yml/badge.svg)](https://github.com/brekkylab/backlot/actions/workflows/ci.yml)
[![python](https://img.shields.io/pypi/pyversions/backlot)](https://pypi.org/project/backlot/)
[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](https://github.com/brekkylab/backlot/blob/main/LICENSE)
[![Discord](https://img.shields.io/badge/Discord-5865F2?logo=discord&logoColor=white)](https://discord.gg/XCSsxYH6R)
[![X](https://img.shields.io/badge/Tweet-000000?logo=x&logoColor=white)](https://x.com/brekkylab)

**Run enterprise SaaS APIs locally.**

Backlot is a local emulator for Slack, Gmail, Google Drive, GitHub, Jira, Notion, S3 and other enterprise APIs. It reproduces the response shapes, pagination, authentication, errors and per-document access controls an integration has to handle, over a deterministic corpus you control — so you build and test against the official vendor SDKs with no vendor account, no OAuth approval, no secrets in CI and no network.

> Think LocalStack, but for the SaaS APIs your connectors and agents talk to.

Why would you need that? Because right now:

- **You can't log in yet**. So you create a workspace, register an app, pick OAuth scopes and wait on an admin who has never heard of you. Zero API calls so far.
- **Then you do it again**. Gmail. Jira. Google Drive. Every source starts over from nothing.
- **So you mock it, and the test passes**. It passes because you wrote both sides of it. It always passes.

Backlot is the side you didn't write. It behaves exactly like the real one.

## Quickstart

```bash
pip install backlot
backlot import --bundled   # a corpus ships with the package; nothing to fetch or write
backlot serve              # http://127.0.0.1:8000
```

Or run one programmatically on a free port:

```python
import backlot
from slack_sdk import WebClient                        # pip install slack_sdk

with backlot.serve() as s:                             # no arguments: a tiny hello-world corpus
    slack = WebClient(token=s.token, base_url=f"{s.base_url}/slack/api/")
    print(slack.conversations_list()["channels"])
```

The only change from talking to the live service is the base URL.

## What it serves

Every source on one local port, each behind the path prefix its own SDK expects.

| Service | Base path |
|---|---|
| Slack | `/slack/api` |
| Gmail | `/gmail/v1` |
| Google Drive (Docs, Sheets, Slides) | `/drive/v3` `/docs/v1` `/sheets/v4` `/slides/v1` |
| GitHub | `/github` |
| Jira | `/atlassian/rest/api` |
| Confluence | `/atlassian/wiki/rest/api` |
| Notion | `/notion/v1` |
| Linear | `/linear/graphql` |
| HubSpot | `/hubspot` |
| Fireflies | `/fireflies/graphql` |
| Amazon S3 | `/s3` |

Responses carry the shapes, pagination, auth and per-document ACLs the real ones have, over a corpus you supply. Every user in the corpus gets a token, so you can assert that one caller's documents never reach another.

## Documentation

| | |
|---|---|
| Every source Backlot serves, and every endpoint of each | [supported-sources.md](https://github.com/brekkylab/backlot/blob/main/docs/supported-sources.md) |
| Building a corpus, and public datasets | [corpus.md](https://github.com/brekkylab/backlot/blob/main/docs/corpus.md) |
| Auth schemes and tokens | [auth.md](https://github.com/brekkylab/backlot/blob/main/docs/auth.md) |
| Every `BACKLOT_*` setting | [configuration.md](https://github.com/brekkylab/backlot/blob/main/docs/configuration.md) |

One runnable script per service, driving the vendor's own SDK, plus MCP, LlamaIndex and mirage
walkthroughs: [examples/](https://github.com/brekkylab/backlot/tree/main/examples).

## Trademarks

Backlot is an independent project, **not affiliated with, endorsed by, or sponsored by** any of the
vendors whose APIs it imitates. Slack, Gmail, Google Drive, Google Docs, Google Sheets, Google
Slides, GitHub, Jira, Confluence, Notion, Amazon S3, HubSpot, Linear and Fireflies are trademarks of
their respective owners, named here only to identify the APIs Backlot serves a compatible subset of.
Backlot ships no vendor logo, wordmark or brand asset.

## License

[MIT](https://github.com/brekkylab/backlot/blob/main/LICENSE)
