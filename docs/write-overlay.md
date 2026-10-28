# Writes, and where they go

[← README](../README.md)

Backlot supports write APIs for some vendors; each source's section of [Supported sources](supported-sources.md) lists the methods that write. A write never reaches the corpus.

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
`chat.update` reads back edited everywhere, search included.

A deleted one goes, unless it is a thread root with replies still under it — real leaves that as
a placeholder so the thread stays readable, `subtype: "tombstone"` and `hidden: true` over
`This message was deleted.`, attributed to `USLACKBOT` rather than to the poster and keeping its
`thread_ts`, `reply_count`, `reply_users` and `latest_reply`. No search finds it, by the
placeholder's words or by the message's own, and `chat.update` and `chat.delete` answer it
`message_not_found` where a reaction and a permalink still work. Deleting the last reply takes
the placeholder with it, and a LIVE root whose last reply goes is a standalone message again.

Two messages posted in one second come back in the order they were posted. Slack's `ts` is a
second and a six-digit fraction, and real's fractions rise within a second as well as across one,
so a posted message takes the next free fraction above the highest its channel already holds in
that second. An imported message keeps the hashed fraction the importer gives it: a corpus states
its own order in its timestamps.

A corpus row cannot be changed, so an edit is stored as an overwrite of one column and a delete as
a tombstone, and both are applied when the row is read. This is why a message the corpus shipped
can be deleted at all. An edited corpus message is dropped from the corpus side of the search
merge — its entry in the corpus index still holds the pre-edit text and cannot be removed — so it
is found once, by what it says now.

`chat.postMessage` honours `thread_ts` and posts a reply in that thread. The arguments it still
ignores are listed in its own docstring; `backlot diff` cannot see them, because it compares query
parameters and Slack's spec declares them as form fields.

A spreadsheet written through `values.update`, `values:batchUpdate`, `values.append`,
`values.clear` or `values:batchClear` reads back written through every Sheets read, and through
Drive: `files.export`
serialises the new cells, fullText search finds them, and `modifiedTime` moves to the write's time.
Real's Drive shows those about a minute after the write; Backlot shows them at once. A spreadsheet
whose corpus record states no grid reads as one sheet of its lines, and a write lands in that grid,
after which it is a grid: real has no spreadsheet that refuses a write.

`USER_ENTERED` reads a typed string the way the Sheets UI does, into a number with a number format
(`12.5%`, `$1,234.50`, `2024-01-15`, `10:30 PM`), a boolean, an error or text, and a cell keeps or
replaces its format by the rules real applies, measured input by input.

`spreadsheets.create` makes a new workbook at the caller's My Drive root, owned by the caller and
shared with nobody: it is listed under `'root' in parents`, and anyone else is refused it as a file
they cannot see. The sheets, grid sizes and sheet properties it states are kept, and so are its
cells. `sheets.copyTo` copies a sheet into a workbook the caller may write, titled in that
workbook's language. `values:batchUpdateByDataFilter` and `values:batchClearByDataFilter` write and
clear what DataFilters match.

`values.append` finds the table it writes below the way real does: from the last cell in the range
holding a value, row by row, grown through every cell in the row or column next to it, and
the values go on the row under it from its first column. `INSERT_ROWS` inserts whole rows there,
and the sheet's row count grows with them.

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
curl http://localhost:8000/_meta/overlay
curl -X POST -H "Authorization: Bearer <admin token>" \
     http://localhost:8000/_meta/overlay/reset
```

`GET /_meta/overlay` is the read a grader makes: the documents written, the grants they carry, the
columns overwritten, the parts replaced (a written spreadsheet's sheets), the documents subtracted,
and the memberships changed — what an agent did, in
one place, without diffing the served surface against itself.

`POST /_meta/overlay/reset` restores the corpus's own state, tombstones included, so a second
evaluation run over the same server starts where the first one did.

Reading is unauthenticated, like the rest of `/_meta`. Resetting requires the admin token.

## What a write is attributed to

A user token writes as that person. The admin/service token writes as `USERVICE0` — the identity
`auth.test` already reports it as — and its messages carry the `bot_message` subtype real Slack
gives an app-posted message.

A spreadsheet is written by its owner alone. Every grant a corpus states makes a reader, and a
reader's write is the 403 real gives one, `The caller does not have permission`; so is the
admin/service token's, which owns only the spreadsheets its service account created, and is
answered as their owner by Drive too.

## What is not modelled

Each is measured, and the ones `backlot diff` can see are recorded in the source's baseline under
`backlot/fidelity/baseline/` with the measurement behind them. The comparison reads a
field list, so the ordering divergence below has no entry to live in and is stated only here.

- **OAuth scopes.** Real Slack splits some answers on scope rather than on identity: posting to a
  public channel you have not joined succeeds with `chat:write.public` and answers
  `not_in_channel` without it. Backlot has no scope concept and takes the permissive branch.
- **An app identity.** Real adds `app_id`, `bot_profile` and `parent_user_id` to the message a
  write echoes back. A corpus has nothing to populate them from. `bot_id` is served on every
  written message whoever wrote it, and on no corpus message: real carries it on everything
  posted through the Web API, over a user token as well as a bot one.
- **Per-reaction times.** Real orders `reactions.list` by when the caller reacted and lists one
  item per reaction, so a message the caller put two emoji on appears twice. A corpus states a
  message's reactions as a set with no times in it, so Backlot orders by the message and lists
  it once.
- **The emoji set.** `reactions.add` takes any name; real answers `invalid_name` for one its
  workspace does not have. Validating it needs Slack's emoji list, and a partial list would
  refuse names real accepts.
- **Formulas.** A `USER_ENTERED` string starting `=` or `+`, or a `formulaValue` a new workbook
  states, is a formula on real, evaluated into its result. Backlot stores the text typed.
- **What a new workbook states and Backlot cannot serve back.** Named ranges, developer metadata,
  data sources, and every cell format but a number format are read and not kept.
- **`spreadsheets.batchUpdate`.** Not served, so a workbook's sheets change only by
  `spreadsheets.create` and `sheets.copyTo`.
- **Rate limits.** Real Sheets refuses a user's writes past 60 a minute with 429
  `RESOURCE_EXHAUSTED`. Backlot does not count them.

## Adding a source

The overlay's tables are generated per source from the registries in `backlot/store.py`, the way
the eleven `<source>_acl` tables already are. Making another source writable is an entry in
`store.WRITABLE`, its overwritable columns in `store.PATCHABLE`, routing that source's readers
through `store.merged_source`, minting ids the way its importer does, and the endpoints themselves.
A source outside `WRITABLE` gets no overlay tables and reads exactly the SQL it read before writes
existed.

A source whose documents keep some of their state in a table of their own (a spreadsheet's sheets
live in `gdrive_sheets`, not on its `gdrive_files` row) names that table in `store.SATELLITES`.
The overlay mirrors it, `store.replace_parts` writes a document's rows there together with the
columns that follow from them, and `store.parts_for` reads the overlay's rows for a document when
it holds any and the corpus's otherwise, never a mix of the two.
