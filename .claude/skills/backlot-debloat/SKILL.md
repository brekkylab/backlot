---
name: backlot-debloat
description: Remove duplication and excess from one area of Backlot — code, prose or tests — without changing what it does, prove that with scripts/invariance.py, open a pull request, and have the invariance and prose reviewers pass it. `/backlot-debloat` runs live; `/backlot-debloat <area> [prose|code|tests]` rehearses locally with no GitHub writes. Run it only when a person or the routine's own prompt says `/backlot-debloat`.
argument-hint: "[area] [prose|code|tests]"
allowed-tools: Read, Edit, Write, Grep, Glob, Bash, Agent
---

# Debloat

You make Backlot smaller without changing it. AGENTS.md is the law of this repository; read it now
if you have not this session. Every run picks one area, finds the duplication and excess in it,
removes one kind of it, proves nothing changed, and hands a pull request to a maintainer.

"Nothing changed" means: every route answers every request with the same status, headers and body;
the CLI, the importer, the MCP tool list and the public `backlot.*` names behave the same; and every
fact a test asserted is still asserted. Private names, module layout and test names are free to move.

Your pull requests are the ones on `claude/debloat-` branches, and only those. Labels, comments,
`/decision`, maintainer access and the shape of a decision comment are exactly as in
`.claude/skills/backlot-loop/SKILL.md` ("Asking for a decision" and the paragraphs before it): read
that part, it binds here too. Everything you write to GitHub is English and ends with its last
sentence, with no signature and no session link.

## Rules

1. **Behaviour does not change.** Not even to fix a bug you find. A divergence or a bug you meet is
   filed as an issue (step 7) and left as it is.
2. **One kind per pull request.** Each kind has its own proof, and a mix proves nothing:
   - `prose` — comments, docstrings and markdown. Proof: `invariance.py prose` says every Python
     file parses to the same tree without docstrings, the docs tests pass, and the said-once map
     accounts for every removed fact.
   - `code` — `backlot/` only; `tests/` is untouched, so the unchanged tests send the same requests
     to the old and the new code. Proof: `invariance.py compare --mode code` finds every response
     identical and every test ending the same way. That proof sees only what the tests reach, so
     every line a `code` cut changes or removes must have run in the base recording (`invariance.py
     uncovered` lists the ones that did not); a cut touching any other line is unproven and is not
     made. A new helper's docstring belongs to the `code` cut that adds the helper.
   - `tests` — `tests/` only; `backlot/` is untouched. Proof: `invariance.py compare --mode tests`
     finds no line or branch of `backlot/` that stopped running, plus the fact map in the body.
3. **Duplicated logic is merged only within one vendor or into vendor-neutral code.** Two vendors'
   helpers that read alike are separate measurements; sharing them means the next fix to one moves
   the other. A shared helper may live in a neutral module (`backlot/pagination.py`,
   `backlot/errors/__init__.py`, `backlot/synth.py`, `tests/_helpers.py`) only when it carries no
   status code, error shape, header or wording a vendor was measured to send.
4. **Copies that differ stay apart.** When two near-copies differ in any detail, do not unify them;
   the difference may be a measurement or a bug, and deciding which is not debloat. When you can
   name a request the two would answer differently, file it; otherwise list it in the body's
   candidates left.
5. **It gets smaller.** Net lines go down. A helper that adds parameters and branches to remove
   fewer lines than it adds is not a cut.
6. **Dead code is code no request can reach, shown from the code.** Coverage is where to look, not
   the proof: a branch no test runs is usually a branch no test sends the request for, and
   deleting it changes behaviour while the recording stays `invariant`. Remove a name only when it
   is not public (`backlot/__init__.py`, or a module `examples/` or the docs import) and nothing
   reads it by name or by string (`git grep -n '<name>'`, including `examples/`, `docs/`,
   `scripts/`); a branch only when its condition cannot hold, and say why in the body.
7. **A test is removed or merged for one of four reasons only**, named in the body for each fact it
   asserted (a test that asserted two facts may have two reasons):
   - *subset*: another test asserts the same fact on the same state — the same corpus or fixture, and
     a request that reaches the same branch of `backlot/`, however else it is spelled;
   - *equivalent case*: a parametrized case in the same equivalence class as a kept case — same
     branch, same answer shape, no boundary of its own;
   - *tautology*: it asserts the mock's own constant, or a library's behaviour rather than Backlot's;
   - *repeated setup*: it builds state another test already builds, and its assertions fold into
     that test.
   Folding into an existing test comes before parametrizing, and parametrizing before a new
   function. A parametrized case gets an `id` that says which behaviour it is. A test stays in the
   file of the layer it tests. "Low value" alone is not a reason.
8. **Prose is said once and stays true.** A fact in two comments, a comment and a doc, or two docs
   is kept in the place nearest the code it describes and removed from the other; between two docs,
   in the one a reader looking the fact up would open — the reference entry for that endpoint or
   setting over an overview. A rewording that saves no line is not a cut. A sentence the cut already
   edits that the code beside it contradicts may be corrected to match the code, named in the body,
   when it describes Backlot's own behaviour; one about what a vendor does is filed instead. A
   docstring that attributes a shape to the vendor is a source attribution: it may move or merge,
   never vanish. A comment carries the measurement, never the story of how it was found, and never
   "merged from", "previously" or an issue number.
9. **Protected, whatever the kind:** `examples/` (each script is self-contained on purpose, so its
   duplication is not bloat), `backlot/schemas/*.schema.json`, `backlot/data/`, the generated blocks
   of `docs/supported-sources.md` and `skills/backlot/SKILL.md`, the fidelity baseline, `AGENTS.md`,
   `CONTRIBUTING.md`, `.github/`, `.claude/`, `pyproject.toml` and `uv.lock`. A test or
   parametrized case that proves which principal may read something — a scoped token refused or
   answered, an admin token answered — is never removed. A refusal of a missing or unusable
   credential is authentication, not that, and falls under rule 7.
10. **Worth a review, and small enough for one sitting.** At most 800 changed lines (added plus
    removed), stopping at a coherent boundary rather than at the limit. At least 30 lines net or one
    test removed; below that, the cuts go in the summary as candidates and no pull request is opened.
11. **Nothing worth doing is a result.** If the area holds no cut that these rules allow, say so and
    stop; never make a change to have made one.

## Areas

| Area | Code | Tests |
|---|---|---|
| `slack` | `backlot/routers/slack.py` | `tests/test_slack.py` |
| `google` | `backlot/routers/google.py`, `backlot/errors/google.py`, `backlot/protojson.py`, `backlot/sheets_grid.py` | `tests/test_google.py` |
| `github` | `backlot/routers/github.py`, `backlot/errors/github.py` | `tests/test_github.py` |
| `atlassian` | `backlot/routers/atlassian.py`, `backlot/errors/atlassian.py` | `tests/test_atlassian.py` |
| `s3` | `backlot/routers/s3.py`, `backlot/errors/s3.py`, `backlot/sigv2.py`, `backlot/sigv4.py`, `backlot/sigv4a.py` | `tests/test_s3.py` |
| `hubspot` | `backlot/routers/hubspot.py` | `tests/test_hubspot.py` |
| `notion` | `backlot/routers/notion.py` | `tests/test_notion.py` |
| `linear` | `backlot/graphql/linear_resolvers.py`, `backlot/graphql/linear_filters.py` | `tests/test_linear.py` |
| `fireflies` | `backlot/routers/fireflies.py`, `backlot/graphql/fireflies_resolvers.py` | `tests/test_fireflies.py` |
| `graphql` | `backlot/graphql/engine.py`, `backlot/graphql/mcp_tools.py` | `tests/test_graphql.py` |
| `store` | `backlot/store.py`, `backlot/pagination.py`, `backlot/synth.py` | `tests/test_store.py`, `tests/test_pagination.py`, `tests/test_synth.py`, `tests/test_search.py` |
| `importer` | `backlot/importer/`, `backlot/validation.py` | `tests/test_importer_byo.py`, `tests/test_importer_erb.py`, `tests/test_schema.py` |
| `auth` | `backlot/auth.py`, `backlot/acl.py`, `backlot/oauth.py`, `backlot/routers/oauth.py` | `tests/test_auth.py`, `tests/test_acl.py` |
| `surface` | `backlot/main.py`, `backlot/server.py`, `backlot/cli.py`, `backlot/config.py`, `backlot/openapi.py`, `backlot/mcp.py`, `backlot/integrations/` | `tests/test_server.py`, `tests/test_cli.py`, `tests/test_openapi.py`, `tests/test_mcp.py`, `tests/test_integrations.py`, `tests/test_llamaindex.py`, `tests/test_sdk.py`, `tests/test_cross_vendor.py` |
| `fidelity` | `backlot/fidelity/` | `tests/test_fidelity.py` |
| `shared-tests` | — | `tests/conftest.py`, `tests/_helpers.py`, and helpers repeated across test files |
| `docs` (`prose` only) | — | `README.md`, `docs/*.md` except the generated `docs/supported-sources.md` |

A cut may reach outside the area's files when it removes a duplicate whose other copy is in the
area — a helper in two test files moving to `tests/_helpers.py` — but never more than that.

Two paths in `code` kind fall outside what the traffic recording sees, and need their own proof on
top of it: the importer's output (build a DB from `examples/bring-your-own-corpus/sample_corpus.jsonl`
with `backlot import` at the base and the head and compare `sqlite3 <db> .dump`, minus any line that
differs between two base imports), and the CLI (run each changed command at both and diff its output
and exit code). Say in the body which you ran.

## Rehearsal

`$0` set (`/backlot-debloat hubspot`, or `/backlot-debloat hubspot tests` to fix the kind) means
rehearsal on that area. You read GitHub freely and write to it never: no issue, label, comment,
push or pull request. Skip the survey's choice of area, but leave the closed files (step 1) alone as
a live run would, so the rehearsal makes only cuts a live run could, and list them with the pull
requests that close them in your summary.

The branch is `claude/rehearsal-debloat-<area>-<MMDDHHMM>`, cut from the HEAD of the branch you
started on, which is the base for every proof and for the reviewers. End by printing the pull
request title and body you would have opened, the issues you would have filed, and each reviewer's
last verdict, then `git checkout` the branch you started on, leaving the rehearsal branch in place.

## Working notes

- Shell state does not survive between commands. When step 2 sets `BASE` and `REC`, print them and
  spell their values out in every later command.
- In zsh, `$BASE:t` is a path modifier: write `"$BASE":tests/…` when you mean a revision and a path.
- `pyproject.toml` already passes `-q` to pytest; adding another hides the summary line with the
  pass count. Run `uv run pytest <files>` as it is.

## 1. Survey

```bash
gh pr list -R brekkylab/backlot --state open --json number,title,labels,isDraft,headRefName,mergeStateStatus,files --limit 100
gh pr list -R brekkylab/backlot --label debloat --state all --json headRefName,createdAt,state --limit 100
```

Your own open pull requests come first. One with an unanswered review, a failing check or a merge
conflict is worked exactly as in step 2 of the loop's skill, re-running the proof (step 5) after any
change and the reviewers (step 8) after any fix, and that is the run. If two debloat pull requests
are already open, do nothing else: a maintainer has enough to read.

Otherwise pick the area. A file is closed while a pull request in review — open, not a draft, not
labelled `hold` — touches it, since a cut there would conflict with work a maintainer is about to
merge; the run leaves closed files alone and works the rest of its area. Each closed file, with the
pull requests that close it:

```bash
gh pr list -R brekkylab/backlot --state open --json number,isDraft,labels,files --limit 100 \
  --jq '[.[] | select(.isDraft | not) | select([.labels[].name] | index("hold") | not) | {n: .number, f: .files[].path}] | group_by(.f)[] | "\(.[0].f) \(map(.n))"'
```

An area whose every file is closed is skipped. Of the rest, take the one whose last debloat pull
request (its branch is `claude/debloat-<area>-<MMDD>`) is oldest, an area never debloated counting
as oldest of all; among those, the one with the most lines. A `routine-fire-payload` naming an area
means that area; if every file of it is closed, say which pull requests close it and stop.

## 2. Branch and start the recordings

Live: `git fetch origin main`, then `git checkout -b claude/debloat-<area>-<MMDD> origin/main`.
Rehearsal: `git checkout -b claude/rehearsal-debloat-<area>-<MMDDHHMM>` from where you are. Then fix
the base and a scratch directory once for the whole run, and print both:

```bash
BASE=$(git rev-parse HEAD); REC=$(mktemp -d /tmp/debloat-XXXXXX); echo "BASE=$BASE REC=$REC"
```

Unless the kind is already known to be `prose`, record the base now, in the background, while you
read: twice, since a `code` cut needs the second as calibration, from a worktree with its own
environment and the script from your branch. Each recording takes two to three minutes.

```bash
git worktree add $REC/tree $BASE && cd $REC/tree && uv sync --all-extras -q \
  && uv run python <your working tree>/scripts/invariance.py record $REC/base \
  && uv run python <your working tree>/scripts/invariance.py record $REC/base2
```

Creating the branch at the commit you are on and returning to the starting branch at the end are the
only checkouts in the working tree you run in; never check out another commit there, since the
skill and the reviewers are read from it.

## 3. Read the area and choose the cut

Read the area's code files whole. Of its test files, read what the kind needs: all of them for
`tests`; for `code`, the private names they import or reach into
(`grep -nE "from backlot\.[a-z_.]+ import|[a-z_]+\._[a-z]" <files>`), which a `code` cut must keep.
Then list what the rules allow, by kind:

- **code**: functions and blocks repeated within the area or with a neutral module; helpers in two
  places doing the same thing; names nothing reads (rule 6); constants and tables spelled twice.
  Check repeats by text first (`grep -n "def _<name>"` across `backlot/`), then by reading.
- **prose**: the same fact in two comments, a comment that restates the line below it, a docstring
  longer than its function with nothing measured in it, a doc paragraph that a generated table or
  another doc already carries, a comment narrating history (rule 8). For each paragraph, grep its
  rarest three or four words across `*.md` and `backlot/`; a second hit is a candidate.
- **tests**: tests whose assertions are a subset of another's, parametrized cases that land in the
  same branch with the same answer shape, setup rebuilt test after test, helpers repeated across
  test files (`grep -hoE '^def _[a-z_]+' tests/*.py | sort | uniq -c | sort -rn`).

When the base recording is done, run `uv run python scripts/invariance.py uncovered $REC/base <the
area's code paths>`. A `code` cut stays off every line it lists (rule 2). Each listed line is also
residue for step 7: reachable code no test sends the request for, or a test that claims more than
it reaches.

A test candidate is settled by the step-5 comparison, not by reading alone: a line only it executed
shows up there as lost coverage, and that test is not a *subset*.

Among the kinds with open files, pick by a quick estimate of which holds the largest cut, without
working the others out in full, or take the kind the rehearsal named. If no kind reaches rule 10's
floor, list the candidates and stop. Write the candidate list down; the body carries what you left
for a later run.

## 4. Cut

Make the cut one candidate at a time, running the area's test files after each (`uv run pytest
<files>`; for `prose`, `tests/test_docs.py tests/test_skills.py`, once per few candidates): a
failure is a behaviour you missed, so undo that candidate rather than adjusting a test to it. Commit
titles are single declarative sentences describing the new state, with no trailers (AGENTS.md).

`uv run ruff check . && uv run ruff format --check .` and `uv run python scripts/prose_check.py`
must pass; the latter reads only Python, so its silence says nothing about markdown. If a docstring
or a schema the generators read changed, `python scripts/gen_docs.py`. Before the pull request, the
whole suite passes (`uv run pytest`), as AGENTS.md asks; for `code` and `tests` the head recording is
that run.

## 5. Prove

For `code` and `tests`, record the head from your working tree:
`uv run python scripts/invariance.py record $REC/head`. `record` refuses a directory that is not
empty, so a second head recording after a fix goes to a new name (`$REC/head2`). Then by kind:

- `prose`: `uv run python scripts/invariance.py prose $BASE`, and `uv run pytest tests/test_docs.py tests/test_skills.py`.
  Neither sees a fact disappear from prose, and for a markdown-only cut `prose` prints `invariant`
  over zero files, so the said-once map is the proof: for every sentence or comment removed, where
  the same fact is still stated (file and line at the head), or that it restated the code beside it.
  A removed fact with no row is a fact lost: put it back.
- `code`: `uv run python scripts/invariance.py compare $REC/base $REC/head --mode code --calibration $REC/base2`.
- `tests`: `uv run python scripts/invariance.py compare $REC/base $REC/head --mode tests`.

It must print `invariant`. A difference is a behaviour the cut changed: find which candidate, undo
it, and prove again. Never widen the comparison to make it pass. Keep `$REC` until the reviewers
pass, since a fix is proved against the same base recording, then `git worktree remove --force
$REC/tree` and `rm -rf $REC`.

For `tests`, also write the fact map: for every test removed or merged, the reason (rule 7) and the
test and assertion that now carry each fact it asserted. Coverage cannot see a lost assertion, so
prove each row: make a one-line change to `backlot/` that the removed test fails on at the base, then
run the carrier's file at the head (and the whole suite if that passes). Something must fail. Copy
the file to `/tmp` before the change and restore it from the copy, never with `git checkout --`, and
never while a recording is running. A row with no failing head test is a fact you were about to
lose: put the test back, or fold its assertion into the carrier.

## 6. Measure

For the body: `git diff --shortstat $BASE` per kind of path (`backlot/`, `tests/`, markdown), the
test function count (`grep -cE '^\s*(async )?def test_' <files>`) before and after, and the first
two lines `compare` prints, which give each run's outcome counts and wall time; the template's
"Tests" line quotes them.

## 7. File the residue

Search open issues first and comment there instead when one already covers it. Never add `agent`.
The first line of each is `Found while debloating <area>`.

- A bug, a divergence, or a near-copy pair with a request the two answer differently (rules 1 and
  4): its own issue — the fidelity-gap template with `fidelity` when it is about what a vendor
  serves, otherwise a plain issue with `bug`.
- The lines `uncovered` listed that a request can reach: one issue for the area, titled
  `<area>: branches no test reaches`, no label, listing each branch with the request that would
  reach it. These are where a later fix has no test to lean on.

## 8. Open the pull request and review

Push and `gh pr create --assignee @me --label debloat`. Title: `<area>: ` and one sentence saying
what is now true (`hubspot: list and search tests share one corpus per object type`). Body: the
repository's pull request template with every section filled, never hard-wrapped:

- **What changed** — the kind, then the cuts as a list, each naming what was removed and where its
  one remaining copy lives, with counts and call sites read from the head, not remembered; then
  candidates seen and left, one line each.
- **Why this is what the real API does** — nothing the API serves changed: the date, the
  `invariance.py` command and its last lines verbatim, and for `tests` the fact map as a table
  (removed test, reason, surviving test and assertion, the mutation that proved it), for `prose`
  the said-once map.
- **Verification** — the three lines the template asks for, plus the measurement table of step 6.
- Checkboxes: tick the third, which speaks for the lines this pull request writes. Leave the first
  two unticked with `— not applicable: behaviour is unchanged` after each.

Dispatch both reviewers in parallel with the Agent tool, each with the one-line prompt
`Review pull request #<n> of brekkylab/backlot` (rehearsal: `Review branch <branch> against <base>`
followed by the title and body you would have sent):

- `subagent_type: invariance-reviewer`
- `subagent_type: prose-reviewer`

If the Agent tool's list of agent types does not include `invariance-reviewer` (a session started
before the file existed does not), dispatch `subagent_type: general-purpose` with the prompt `Your
instructions are .claude/agents/invariance-reviewer.md; follow them as your system prompt.` followed
by the line above.

Treat findings as the loop's step 9 does: reproduce, then fix or rebut with evidence, prove again
after any fix, and dispatch again in fresh contexts until both pass on the same commit. A finding
that a cut changed behaviour is answered by undoing that cut, never by arguing the change is
harmless. After three rounds with a `block` standing, draft the pull request, add
`needs-maintainer`, and post a decision comment whose options are `merge`, `drop <the disputed cut>`
and `close`.

## 9. Hand over

When both pass and `gh pr checks <n> --watch --fail-fast` is green, add `ready-for-maintainer` and
request review from `brekkylab/backlot-reviewer`. End with one paragraph: the area and kind, what
was cut in lines and tests, the proof's result, the issues filed, the pull request.

## Budget

One run, one pull request. If the survey or step 3 finds nothing the rules allow, say so in one line
and stop.
