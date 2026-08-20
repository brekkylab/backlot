# Backlot

[![tests](https://github.com/brekkylab/backlot/actions/workflows/ci.yml/badge.svg)](https://github.com/brekkylab/backlot/actions/workflows/ci.yml)
[![python](https://img.shields.io/pypi/pyversions/backlot)](https://pypi.org/project/backlot/)
[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)

**Change the base URL. Nothing else.**

Backlot serves enterprise SaaS APIs — Slack, Gmail, Drive, GitHub, Jira, Notion, S3, and
more — with the exact response shapes, pagination, auth and per-document ACLs the real ones have,
over a corpus you supply.

![Importing the bundled corpus, starting the server, then reading a Slack message out of it with curl — beside the same call against the real Slack API, returning the same seven fields.](assets/readme-demo/demo.gif)

<!-- figure: facades — assets/figures/facades-{light,dark}.svg
     alt: Vendor-shaped facades on one localhost port, all backed by a single corpus. -->
**Each facade answers like the real service. Behind all of them is one corpus you supplied — with
no vendor account, OAuth app, rate limit or network in between.**

## Quickstart

```bash
pip install backlot
backlot import --bundled    # the corpus bundled in the package -> data/mock.sqlite + data/tokens.yaml
backlot serve               # http://127.0.0.1:8000
```

Or run one programmatically on a free port:

```python
import backlot
from slack_sdk import WebClient                        # pip install slack_sdk

with backlot.mock_server() as m:                       # no arguments: the bundled corpus
    slack = WebClient(token=m.token, base_url=f"{m.base_url}/slack/api/")
    print(slack.conversations_list()["channels"])
```

## When you need this

- **Building or upgrading a connector** — crawl to exhaustion, then diff what you got against what you loaded
- **Testing ACL-scoped retrieval** — every document carries its own readers, so a leak is a failing test
- **Running that suite in CI** — no accounts, no secrets, no network, no flakes
- **Evaluating RAG or agents** — the same answers every run; ids and timestamps are derived, not random
- **Reproducing a bug you cannot reach** — write the document that causes it and serve it in seconds

## Example Usages

<!-- figure: clients — assets/figures/clients-{light,dark}.svg
     alt: Official SDKs, MCP servers, LlamaIndex readers and mirage all reaching one localhost
          port; only the base URL changes. -->
**The only change from talking to the live service is the base URL.**

| Point this at it | Runnable, one script per service |
|---|---|
| Official vendor SDKs | [`examples/using-official-sdk/`](examples/using-official-sdk/) |
| MCP servers, or the mock's own OpenAPI→MCP bridge | [`examples/using-mcp-with-agents/`](examples/using-mcp-with-agents/) |
| LlamaIndex readers | [`examples/using-llamaindex-readers/`](examples/using-llamaindex-readers/) |
| [mirage](https://github.com/strukto-ai/mirage)'s virtual filesystem | [`examples/using-mirage/`](examples/using-mirage/) |

## Documentation

| | |
|---|---|
| Every endpoint of every service | [docs/endpoints.md](docs/endpoints.md) |
| Building a corpus, and public datasets | [docs/corpus.md](docs/corpus.md) |
| Auth schemes and mock credentials | [docs/auth.md](docs/auth.md) |
| Every `BACKLOT_*` setting | [docs/configuration.md](docs/configuration.md) |

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md). Fidelity to the real APIs is the point, so a divergence is
a bug — measure against the real service, and bring a test that fails without your fix.

## License

[MIT](LICENSE)
