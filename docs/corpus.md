# Preparing a corpus

[← README](../README.md)

The server reads a corpus from `data/` (`mock.sqlite` + `tokens.yaml`), and there are three ways to
put one there: the corpus that ships inside the package, your own documents, or a public dataset.

## The bundled corpus

```bash
backlot import --bundled
```

Nothing to download and nothing to write — it lives in the wheel at
[`backlot/data/hello.jsonl`](../backlot/data/hello.jsonl) and covers **every source**, so every
endpoint answers immediately. It is what `backlot.mock_server()` loads when called with no
arguments, and what the examples and the README's demo run against.

What you get, on the org `acme`:

| Source | Records in the file | Documents served |
|---|---|---|
| `slack` | 22 | 41 |
| `gmail` | 10 | 22 |
| `github` | 14 | 14 |
| `google_drive` | 14 | 14 |
| `linear` | 14 | 14 |
| `confluence` | 11 | 11 |
| `jira` | 12 | 12 |
| `hubspot` | 12 | 12 |
| `s3` | 12 | 12 |
| `notion` | 9 | 9 |
| `fireflies` | 6 | 6 |
| **Total** | **136** | **167** |

The two columns differ because a record is not always one document: a Slack message carrying
`replies` and a Gmail thread carrying its messages each expand on load. `/health` reports both
numbers for the same reason — `source_documents` is what the corpus offered, `documents` is what is
served.

It also ships **10 principals in 7 groups** — `engineering`, `sales`, `handbook`, `product`,
`design`, `leadership`, `support` — with overlapping membership, and ten documents carry an explicit
`readers` list. One person, `sam.ortiz@northwind.example`, is deliberately **outside the org**: an
external collaborator, so "does an outsider see this?" is a question you can ask without building a
corpus first. Every identity and its token comes back from `GET /_mock/users` — see
[auth.md](auth.md).

**It is also a worked example of the format below.** `hello.jsonl` is ordinary BYO input, not a
special internal shape, and it validates as such:

```bash
backlot import backlot/data/hello.jsonl --dry-run   # OK: 136 records valid.
```

So the fastest way to learn what a record looks like for a given source is to read the lines for
that source in a file you already have.

## Bring your own corpus

You describe each document the way its own service would, and a per-source JSON Schema says what
that record may carry. `title` and `content` are served verbatim; so is every other field you set —
authors, timestamps, threads, comments, labels, states, ACLs — so no part of a response *has* to be
synthesized. The facts a document cannot exist without are **required**, not invented: its author,
the container it lives in, and its clock. What the corpus does not own are the **served ids**, and
those are derived **deterministically** — each hashed from the stable key it belongs to (the
record's own identity, a container's name, an author's address) — so an id never moves between
calls or pages.

One JSONL document per line, validated against a per-service JSON Schema
([`backlot/schemas/`](../backlot/schemas/)), then loaded:

```bash
backlot import mycorpus.jsonl              # validate + load -> data/
backlot import mycorpus.jsonl --dry-run    # validate only, no DB writes
backlot import mycorpus.jsonl --roster roster.yaml   # state the principals, don't derive them
backlot import corpus.jsonl.gz             # gzipped, read as a stream
backlot import artifact-dir/               # a sharded corpus + its manifest, digests verified
```

```json
{"source_type": "slack", "channel": "incidents", "author_email": "bob@acme.com", "created": "2026-02-10T18:00:00Z", "content": "Anyone seeing 502s from the gateway?", "replies": [{"content": "Looking now.", "author_email": "ava@acme.com", "created": "2026-02-10T18:00:40Z"}]}
{"source_type": "gmail", "mailbox": "ceo", "title": "Q1 board deck draft", "content": "Draft narrative for the Q1 board meeting.", "author_email": "ceo@acme.com", "created": "2026-01-20T16:00:00Z", "to": "ava@acme.com", "readers": ["ceo@acme.com", "ava@acme.com"]}
```

A record states the facts the served document cannot exist without — who wrote it, where it
lives, when it happened — plus whatever its own vendor always reports. Nothing is filled in behind
your back: `backlot import --dry-run` names every record that leaves one out. Where to look next:

| To learn | Read |
|---|---|
| every field a record may carry | [`examples/bring-your-own-corpus/sample_corpus.jsonl`](../examples/bring-your-own-corpus/sample_corpus.jsonl) — the field reference, and a test keeps it exhaustive |
| the rules each source imposes | [`backlot/schemas/README.md`](../backlot/schemas/README.md) |
| import → serve → query, runnable | [`examples/bring-your-own-corpus/run.py`](../examples/bring-your-own-corpus/run.py) |

The schemas double as the contract for **LLM dataset generation**: hand one to a model as a
structured-output schema, generate records, then `--dry-run` before loading. See
[`backlot/schemas/README.md`](../backlot/schemas/README.md).

## Load a public dataset

[EnterpriseRAG-Bench](https://github.com/onyx-dot-app/EnterpriseRAG-Bench) is ~500k synthetic
enterprise documents across nine of the supported sources. One command downloads, loads and
ACL-derives it:

```bash
backlot import --type enterpriserag-bench
```

What that dataset does and does not carry, and how to redistribute it as BYO-JSONL, is in
[`examples/import-enterpriserag-bench/`](../examples/import-enterpriserag-bench/) — it is one corpus
you can load, not part of this server's contract.
