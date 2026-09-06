# Writes, and where they go

[← README](../README.md)

Backlot serves writes on Slack. They never reach the corpus.

## The corpus is not writable, and that is enforced

The serving connection is opened `mode=ro` on the corpus file. SQLite applies that to the main
database alone, so a second database **attached** to the same connection takes writes while the
corpus stays untouchable — a write to it raises `attempt to write a readonly database` rather than
relying on nobody trying. A test hashes the corpus file before and after a run of writes and
compares.

That attached database is the **overlay**. It lives in memory, belongs to one server process, and
is gone when the process is.

## What a read sees

Reads merge the two. A message posted through `chat.postMessage` appears in
`conversations.history`, `conversations.replies` and `search.messages`; one edited through
`chat.update` reads back edited everywhere, search included; one deleted through `chat.delete`
disappears from all of them.

A corpus row cannot be changed, so an edit is stored as an overwrite of one column and a delete as
a tombstone, and both are applied when the row is read. This is why a message the corpus shipped
can be deleted at all.

Search needed one thing more. Relevance is bm25, whose inverse document frequency is a property of
the index a term is in — so an overlay holding a handful of rows scores everything in it at about
`-1e-06`, where the corpus index scores comparable text `-2.70`. Lower ranks higher, so a newly
posted message would sort behind every corpus hit however well it matched. Overlay rows are matched
by an FTS5 index of their own and then **scored against the corpus index's statistics**, which puts
both sides on one scale.

## Reading and resetting it

Two endpoints, in the `/_meta` namespace Backlot uses for its own affordances rather than a
vendor's:

```bash
curl http://localhost:8000/_meta/overlay              # every row written since the last reset
curl -X POST http://localhost:8000/_meta/overlay/reset # throw them all away
```

`GET /_meta/overlay` is the read a grader makes: the documents written, the grants they carry, the
columns overwritten, the documents subtracted, and the memberships changed — what an agent did, in
one place, without diffing the served surface against itself.

`POST /_meta/overlay/reset` restores the corpus's own state, tombstones included, so a second
evaluation run over the same server starts where the first one did.

Both are unauthenticated, like the rest of `/_meta`.

## What a write is attributed to

A user token writes as that person. The admin/service token writes as `USERVICE0` — the identity
`auth.test` already reports it as — and its messages carry the `bot_message` subtype real Slack
gives an app-posted message.

## Scopes are not modelled

Real Slack splits some answers on OAuth scope rather than on identity: posting to a public channel
you have not joined succeeds with `chat:write.public` and answers `not_in_channel` without it.
Backlot has no scope concept and takes the permissive branch. The divergence is recorded in
`backlot/fidelity/baseline/slack.json` rather than left for a caller to discover.

## Adding a source

The overlay's tables are generated per source from the registries in `backlot/store.py`, the way
the eleven `<source>_acl` tables already are. Making another source writable is an entry in
`store.WRITABLE`, its overwritable columns in `store.PATCHABLE`, routing that source's readers
through `store.merged_source`, minting ids the way its importer does, and the endpoints themselves.
A source outside `WRITABLE` gets no overlay tables and reads exactly the SQL it read before writes
existed.
