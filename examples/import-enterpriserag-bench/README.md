# Import from EnterpriseRAG-Bench

[EnterpriseRAG-Bench](https://github.com/onyx-dot-app/EnterpriseRAG-Bench) ships structured
``generated_data/`` — real owners/authors/dates/participants/ACL signals per doc, across **all
nine** of the sources it ships (Slack, Gmail, Linear, Google Drive, HubSpot, Fireflies, GitHub,
Jira, Confluence — 511,962 documents). One command downloads it, loads
it into the per-service tables, derives the ACL from the real people/scope fields, and writes
``tokens.yaml`` for the resolved roster:

```bash
backlot import --type enterpriserag-bench   # download -> load -> ACL. No options.
```

There is nothing to tune. The download is cached under `BACKLOT_RAW_DIR`, and the import returns
that cache when it is populated, so re-running does not refetch.

This is faithful representation, not synthesis: names are resolved to real emails via the
employee directory (``backlot.importer.principals``), and **every import parses the real
conversations embedded in the content** (Slack transcripts → threads, GitHub PR reviews / Jira
comments → real comments, Gmail threads → per-email messages, Fireflies transcripts →
per-sentence utterances with speakers and timings).

## Walkthrough

`run.py` runs the import into `examples/import-enterpriserag-bench/data` (downloading on the
first run; cached after), starts a real server against it, and prints what got served:

```bash
python examples/import-enterpriserag-bench/run.py
```

`BACKLOT_RAW_DIR` and `BACKLOT_DATASET_REPO` configure the download; they live on
`backlot.importer.erb.BenchSettings` rather than in the settings the rest of the server reads. The
cache defaults to `./data/raw` even when `BACKLOT_DATA_DIR` points elsewhere, so a build into a
throwaway directory reuses the ~1 GB you already downloaded.

## Serving it from Docker

Import on the host, then mount the result over the corpus-less image. The Dockerfile has no
knowledge of this dataset — it bakes only the corpus that ships in the package — so a download this
size stays where you can see it, resume it and cache it:

```bash
backlot import --type enterpriserag-bench   # host: downloads + builds ./data (cached in ./data/raw)

docker build --target serve -t backlot .    # the server, no corpus
docker run -p 8000:8000 -v "$PWD/data:/app/data" backlot
```

## What this dataset does and does not carry

Properties of the data, not of the server — they apply only when you load this corpus.

- **Notion and S3 are not in it.** Those two sources arrive only through a BYO corpus.
- **HubSpot** ships as company (account) records only, whose CRM notes are imported as first-class
  `notes` objects associated with the company; contacts/deals/tickets arrive via BYO. Its
  `linked_*` fields are free-text stubs referencing other sources, so they stay properties rather
  than becoming associations. Transcript and record visibility is org-wide plus a per-user grant for
  everyone who resolves: the corpus names far more people than can authenticate, so an
  owner-or-channel scope would leave most documents readable by the admin token alone.
- **Linear** is its third-largest source (35,308 issues). Its `P0`-`P3` priorities are mapped onto
  Linear's own 0-4 scale and its `status` onto `state`, so the served payload speaks Linear's
  vocabulary rather than this dataset's. 5,055 of its issue keys repeat, so `issue(id: "ENG-123")`
  resolves a repeated identifier to the first match while the UUID form is always exact.
- **Fireflies** ships 10,173 transcripts as **one flat text blob per meeting** — not structured
  per-sentence records — so the sentences the API serves are *parsed* from it (~619k of them) across
  the six line formats the corpus uses, gated on each meeting's declared attendees so a transcript's
  auto-notes header (`Date:`, `Duration:`) cannot mint a speaker. Only *start* times are in the data
  (99.9% of lines): end times are derived, wall-clock transcripts are rebased onto elapsed time, and
  a garbled reading is dropped rather than tearing a 50-hour hole in a 60-minute meeting. Its
  `meeting_id` is **not unique**, so it is served as `calendar_id` and `Transcript.id` is
  synthesized. The parser and these decisions live in `backlot/importer/erb.py`.
- **Slack speakers are not in `users.list`.** This corpus generates transcript speakers
  independently of the employee directory, so the two are largely disjoint: of **74,138** distinct
  message authors only **3,971 (5.4%)** are registered user principals, and all **70,167** of the
  rest are on the org's own domain. 74k speakers against an 11,913-person directory is not a
  headcount any real workspace has.

  Backlot does not paper over it. `users.list` serves the directory, so **an author outside it
  resolves through `users.info` but never appears in `users.list`** — a combination real Slack
  cannot produce, and the one place a client written against Backlot will behave differently in
  production. What is available instead: `conversations.members` pages the channel's own speakers,
  so every author of a channel is discoverable there even when the roster omits them. Reconciling
  the two sets means either inventing ~70k colleagues or discarding the transcripts' own speakers,
  so it is a decision about the dataset rather than about this server.

## Redistributing it as a BYO corpus

`backlot export` writes the whole thing as BYO-JSONL instead of a database, losslessly — see
[**Round-tripping an existing dataset**](../../backlot/schemas/README.md#round-tripping-an-existing-dataset).
