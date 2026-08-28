# Backlot

[![tests](https://github.com/brekkylab/backlot/actions/workflows/ci.yml/badge.svg)](https://github.com/brekkylab/backlot/actions/workflows/ci.yml) [![python](https://img.shields.io/pypi/pyversions/backlot)](https://pypi.org/project/backlot/) [![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE) [![Discord](https://img.shields.io/badge/Discord-5865F2?logo=discord&logoColor=white)](https://discord.gg/XCSsxYH6R) [![X](https://img.shields.io/badge/Tweet-000000?logo=x&logoColor=white)](https://x.com/brekkylab)

**Serve your own enterprise playground**

Backlot is a local emulator for enterprise SaaS APIs. Test your Slack, Gmail, Drive and the rest of your integrations.

**No account. No OAuth. No network.**

![Connecting Slack to an app, two ways, side by side. On the left, the real Slack API: create a workspace, register an app, add a bot user, pick OAuth scopes, register a redirect URL, install to the workspace — then wait on an admin to approve it, with still zero API calls made. On the right, Backlot: pip install, backlot serve, and one changed base URL, after which a conversations.history response comes back with the same fields, pagination and per-document ACLs. A day gone, against seconds.](assets/demo.gif)

Why would you need that? Because right now:

- **You can't log in yet**. So you create a workspace, register an app, pick OAuth scopes and wait on an admin who has never heard of you. Zero API calls so far.
- **Then you do it again**. Gmail. Jira. Drive. Every source starts over from nothing.
- **So you mock it, and the test passes**. It passes because you wrote both sides of it. It always passes.

Backlot is the side you didn't write. It fails where the real one fails.

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

with backlot.mock_server() as m:                       # no arguments: the bundled corpus
    slack = WebClient(token=m.token, base_url=f"{m.base_url}/slack/api/")
    print(slack.conversations_list()["channels"])
```

The only change from talking to the live service is the base URL.

## What it serves

Every source on one local port, each behind the path prefix its own SDK expects.

<picture><source media="(prefers-color-scheme: dark)" srcset="assets/figures/architecture.png"><img alt="Your code on the left, calling the Slack and GitHub SDKs with only their base URL changed. In the middle, one local server on 127.0.0.1:8000, listing the services it answers as and the path prefix each is served under. On the right, the single SQLite corpus behind all of them." src="assets/figures/architecture-light.png"></picture>

Every row links a runnable example that drives that service the way the vendor documents it, using the official SDK where one exists.

| Service | Base path | Example |
|---|---|---|
| Slack | `/slack/api` | [`slack.py`](examples/using-official-sdk/slack.py) |
| Gmail | `/gmail/v1` | [`gmail.py`](examples/using-official-sdk/gmail.py) |
| Google Drive (Docs, Sheets, Slides) | `/drive/v3` `/docs/v1` `/sheets/v4` `/slides/v1` | [`gdrive.py`](examples/using-official-sdk/gdrive.py) |
| GitHub | `/github` | [`github.py`](examples/using-official-sdk/github.py) |
| Jira | `/atlassian/rest/api` | [`jira.py`](examples/using-official-sdk/jira.py) |
| Confluence | `/atlassian/wiki/rest/api` | [`confluence.py`](examples/using-official-sdk/confluence.py) |
| Notion | `/notion/v1` | [`notion.py`](examples/using-official-sdk/notion.py) |
| Linear | `/linear/graphql` | [`linear/`](examples/using-official-sdk/linear/) |
| HubSpot | `/hubspot` | [`hubspot.py`](examples/using-official-sdk/hubspot.py) |
| Fireflies | `/fireflies/graphql` | [`fireflies.py`](examples/using-official-sdk/fireflies.py) |
| Amazon S3 | `/s3` | [`s3.py`](examples/using-official-sdk/s3.py) |

**Next up:** Google Chat, Microsoft Graph, Box, Dropbox, DocuSign.

The full roadmap lives in [#89](https://github.com/brekkylab/backlot/issues/89) — ask there for the one you need.

## When you need this

- 🔌 **Building or upgrading an integration**. The cursors, page shapes and error bodies the real API returns, without an account to get them from.
- 🧪 **Testing it, and keeping it tested**. One fixture on your laptop and in CI, with no secrets and nothing to flake. Every user in the corpus gets a token, so you can also assert that one caller's documents never reach another.
- 🛠️ **Building before you have credentials**. A Slack-shaped, Drive-shaped world to develop against before anyone hands you production credentials.
- 🤖 **Evaluating a RAG pipeline or an agent**. The same corpus, the same ids and the same answers on every run, so a score that moves means your code moved.
- 🐛 **Reproducing a bug in data you can't see**. A document inside someone else's workspace breaks your parser. Write one shaped like it, serve it, and keep the failing test.

## Examples

| Point this at it | Runnable |
|---|---|
| 📦 Official vendor SDKs, one script per service | [`examples/using-official-sdk/`](examples/using-official-sdk/) |
| 🔗 MCP servers, or Backlot's own OpenAPI→MCP and GraphQL→MCP bridges | [`examples/using-mcp-with-agents/`](examples/using-mcp-with-agents/) |
| 🦙 Load it as documents, with the official [LlamaIndex](https://docs.llamaindex.ai/en/stable/module_guides/loading/connector/) readers | [`examples/using-llamaindex-readers/`](examples/using-llamaindex-readers/) |
| 🗂️ Read it with `ls`, `cat` and `grep`, over [mirage](https://github.com/strukto-ai/mirage)'s virtual filesystem | [`examples/using-mirage/`](examples/using-mirage/) |
| 📥 Your own corpus, from a JSONL file | [`examples/bring-your-own-corpus/`](examples/bring-your-own-corpus/) |

<details><summary>Or drop it straight into your test suite — <code>mock_server()</code> as a pytest fixture</summary>

```python
# conftest.py — mock_server() picks a free port and tears itself down
import backlot
import pytest

@pytest.fixture(scope="session")
def enterprise():
    with backlot.mock_server() as m:
        yield m


# test_slack.py
from slack_sdk import WebClient

def test_history_paginates(enterprise):
    slack = WebClient(token=enterprise.token, base_url=f"{enterprise.base_url}/slack/api/")
    channel = slack.conversations_list()["channels"][0]["id"]

    page = slack.conversations_history(channel=channel, limit=2)

    assert page["has_more"]
    assert page["response_metadata"]["next_cursor"]
```

</details>

## Documentation

| | |
|---|---|
| Every source Backlot serves, and every endpoint of each | [docs/supported-sources.md](docs/supported-sources.md) |
| Building a corpus, and public datasets | [docs/corpus.md](docs/corpus.md) |
| Auth schemes and mock credentials | [docs/auth.md](docs/auth.md) |
| Every `BACKLOT_*` setting | [docs/configuration.md](docs/configuration.md) |
| Vendor names and trademarks | [NOTICE.md](NOTICE.md) |

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md). Fidelity to the real APIs is the point, so a divergence is a bug — measure against the real service, and bring a test that fails without your fix.

## License

[MIT](LICENSE)
