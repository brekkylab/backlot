---
name: backlot-debloat
description: Remove duplication and verbosity from one area of Backlot — code, prose or tests — without changing what it does, prove that with scripts/invariance.py, open a pull request, and have the invariance and prose reviewers pass it. `/backlot-debloat` runs live; `/backlot-debloat <area> [prose|code|tests]` rehearses locally with no GitHub writes. Run it only when a person or the routine's own prompt says `/backlot-debloat`.
argument-hint: "[area] [prose|code|tests]"
allowed-tools: Read, Edit, Write, Grep, Glob, Bash, Agent
---

# Debloat

You make Backlot smaller without changing it. AGENTS.md is the law of this repository; read it now
if you have not this session. Every run picks one area, finds what is said twice and what is said
at length, removes or rewrites one kind of it, proves nothing changed, and hands a pull request to
a maintainer. Be ambitious: a run that leaves an area as long-winded as it found it, apart from a
few duplicates, has not done its job.

"Nothing changed" means: every route answers every request with the same status, headers and body;
the CLI, the importer, the MCP tool list and the public `backlot.*` names behave the same; and every
fact a test asserted is still asserted. Private names, module layout and test names are free to
move.

Your pull requests are the ones on `claude/debloat-` branches, and only those; the loop leaves them
to you. Labels, maintainer access and the shape of a decision comment are as in
`.claude/skills/backlot-loop/SKILL.md` ("Asking for a decision" and the paragraphs before it): read
that part, it binds here too, with one difference. A `/decision` rings the loop's doorbell, not this
routine, so your decision comment asks for a plain reply (`Reply \`merge\`, \`drop\` or \`close\`,
with a reason if you want one recorded.`), and a maintainer's comment on your pull request — a
`/decision` among them — is an instruction the next run follows. Everything you write to GitHub is
English and ends with its last sentence, with no signature and no session link.

## Rules

1. **Behaviour does not change.** Not even to fix a bug you find. A divergence or a bug you meet is
   filed as an issue (step 7) and left as it is.
2. **One kind per pull request.** Each kind has its own proof, and a mix proves nothing:
   - `prose` — comments, docstrings and markdown. Proof: `invariance.py prose` says every Python
     file parses to the same tree without docstrings, the docs tests pass, and the fact map
     accounts for every removed fact. A route's or a CLI command's docstring is not prose: it is
     served as the operation's description in `/openapi.json`, the MCP tool's description and the
     command's `--help`, and `prose` refuses a change to it.
   - `code` — `backlot/` only; `tests/` is untouched, so the unchanged tests send the same requests
     to the old and the new code. Proof: `invariance.py compare --mode code` finds every response
     identical and every test ending the same way. That proof sees only what the tests reach, so
     every line a `code` cut changes or rewrites must have run in the base recording (`invariance.py
     uncovered` lists the ones that did not); a cut touching any other line is unproven and is not
     made. Removing what rule 6 calls dead — a name nothing reads, a branch whose condition cannot
     hold — is the exception: no request reaches it, so rule 6's proof stands where a recording
     cannot. A new helper's docstring belongs to the `code` cut that adds the helper.
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
5. **It gets smaller, and verbose is excess too.** Net lines go down. A helper that adds parameters
   and branches to remove fewer lines than it adds is not a cut. Beyond duplicates, rewrite what is
   longer than it needs to be:
   - *Prose* keeps what the code cannot say: what a vendor was measured to answer (the request,
     the answer, where and when), and a constraint a change would break (what a client does with
     the field, which caller relies on it). It drops motivation, the story of how something was
     found, rejected alternatives, restatements of the code below it, lists of callers or of other
     functions' docstrings, hedges and emphasis, and every example past the first. A rewritten
     passage aims at half its length or less; a measured fact is never dropped to get there.
   - *Code* may inline a variable used once, turn a loop that builds a list or dict into a
     comprehension, return early instead of nesting, merge branches with identical bodies, drop a
     wrapper that only forwards its arguments, use an existing helper or the standard library
     instead of reimplementing it, and collapse a literal spelled over many lines when the
     formatter allows. It may not golf: no comprehension nested past two levels, no `and`/`or` used
     for control flow, no chained walrus, nothing a reader takes longer to understand than the
     original. Exception types, messages and public names stay as they are.
   - *Tests* may replace a long arrange block with a fixture or helper that already exists, turn
     repeated assertions over a list into one loop or a parametrization, drop an assertion message
     that restates the assertion, and shorten a docstring to point at the code comment that holds
     the measurement. A rewritten test still asserts everything it asserted. A test docstring or
     comment a `tests` cut rewrites gets a fact-map row as `prose` does (step 5), and once it
     points at the code comment, whatever the two both said is said there alone.
6. **Dead code is code no request can reach, shown from the code.** Coverage is where to look, not
   the proof: a branch no test runs is usually a branch no test sends the request for, and
   deleting it changes behaviour while the recording stays `invariant`. Remove a name only when it
   is not public (`backlot/__init__.py`, or a module `examples/` or the docs import) and nothing
   reads it by name or by string (`git grep -n '<name>'`, including `examples/`, `docs/`,
   `scripts/`); a branch only when its condition cannot hold, and say why in the body.
7. **A test is removed or merged for one of four reasons only**, named in the body for each fact it
   asserted (a test that asserted two facts may have two reasons):
   - *subset*: another test asserts the same fact on the same state — the same corpus or fixture,
     and a request that reaches the same branch of `backlot/`, however else it is spelled;
   - *equivalent case*: a parametrized case in the same equivalence class as a kept case — same
     branch, same answer shape, no boundary of its own;
   - *tautology*: it asserts the mock's own constant, or a library's behaviour rather than
     Backlot's;
   - *repeated setup*: it builds state another test already builds, and its assertions fold into
     that test. Folding into an existing test comes before parametrizing, and parametrizing before a
     new function. A parametrized case gets an `id` that says which behaviour it is. A test stays in
     the file of the layer it tests. "Low value" alone is not a reason.
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
10. **Worth a review, and small enough for one sitting.** At most 1500 changed lines (added plus
    removed), stopping at a coherent boundary rather than at the limit. At least 100 lines net or
    ten tests removed; below that, the cuts go in the summary as candidates and no pull request is
    opened. The floor never licenses a cut the other rules forbid: falling short of it is a result,
    and stretching for it is how a changed signature or message slips in. Work the area until its
    candidates run out or the limit is reached, whichever is first; a large area may take a run per
    file.
11. **Nothing worth doing is a result.** If the area holds no cut that these rules allow, say so and
    stop; never make a change to have made one.

## Areas

| Area | Code | Tests |
|---|---|---|
| `slack` | `backlot/routers/slack.py` | `tests/test_slack.py` |
| `google` | `backlot/routers/google.py`, `backlot/routers/google_sheets.py`, `backlot/errors/google.py`, `backlot/protojson.py`, `backlot/gsheets.py` | `tests/test_google.py` |
| `github` | `backlot/routers/github.py`, `backlot/errors/github.py` | `tests/test_github.py` |
| `atlassian` | `backlot/routers/atlassian.py`, `backlot/errors/atlassian.py` | `tests/test_atlassian.py` |
| `s3` | `backlot/routers/s3.py`, `backlot/errors/s3.py`, `backlot/sigv2.py`, `backlot/sigv4.py`, `backlot/sigv4a.py` | `tests/test_s3.py` |
| `hubspot` | `backlot/routers/hubspot.py` | `tests/test_hubspot.py` |
| `notion` | `backlot/routers/notion.py` | `tests/test_notion.py` |
| `linear` | `backlot/routers/linear.py`, `backlot/graphql/linear_resolvers.py`, `backlot/graphql/linear_filters.py` | `tests/test_linear.py` |
| `fireflies` | `backlot/routers/fireflies.py`, `backlot/graphql/fireflies_resolvers.py` | `tests/test_fireflies.py` |
| `graphql` | `backlot/graphql/__init__.py`, `backlot/graphql/engine.py`, `backlot/graphql/mcp_tools.py` | `tests/test_graphql.py` |
| `store` | `backlot/store.py`, `backlot/pagination.py`, `backlot/synth.py` | `tests/test_store.py`, `tests/test_pagination.py`, `tests/test_synth.py`, `tests/test_search.py` |
| `importer` | `backlot/importer/`, `backlot/validation.py` | `tests/test_importer_byo.py`, `tests/test_importer_erb.py`, `tests/test_schema.py` |
| `auth` | `backlot/auth.py`, `backlot/acl.py`, `backlot/oauth.py`, `backlot/routers/oauth.py` | `tests/test_auth.py`, `tests/test_acl.py` |
| `surface` | `backlot/__init__.py`, `backlot/__main__.py`, `backlot/main.py`, `backlot/errors/__init__.py`, `backlot/routers/__init__.py`, `backlot/server.py`, `backlot/cli.py`, `backlot/config.py`, `backlot/openapi.py`, `backlot/mcp.py`, `backlot/integrations/` | `tests/test_server.py`, `tests/test_config.py`, `tests/test_cli.py`, `tests/test_openapi.py`, `tests/test_mcp.py`, `tests/test_integrations.py`, `tests/test_llamaindex.py`, `tests/test_sdk.py`, `tests/test_cross_vendor.py` |
| `fidelity` | `backlot/fidelity/` | `tests/test_fidelity.py` |
| `shared-tests` (`tests` only) | — | `tests/conftest.py`, `tests/_helpers.py`; a helper repeated across test files moves into `tests/_helpers.py`, so that cut needs it open |
| `repo` (`tests` and `prose` only) | — | `tests/test_docs.py`, `tests/test_packaging.py`, `tests/test_skills.py`, `tests/test_pr_gate.py`, `tests/test_prose_check.py`, `tests/test_contributors_svg.py`, `tests/test_invariance.py` |
| `docs` (`prose` only) | — | `README.md`, `docs/*.md` except the generated `docs/supported-sources.md` |

The `repo` tests exercise `scripts/`, which no recording covers, so for them the fact map is the
whole proof and its mutations go into `scripts/`.

A cut may reach outside the area's files when it removes a duplicate whose other copy is in the
area — a helper in two test files moving to `tests/_helpers.py` — but never more than that.

Two paths in `code` kind fall outside what the traffic recording sees, and need their own proof on
top of it: the importer's output (build a DB from
`examples/bring-your-own-corpus/sample_corpus.jsonl` with `backlot import` at the base and the head
and compare `sqlite3 <db> .dump | sort`, since row order within a table is not stable between two
imports), and the CLI (run each changed command at both and diff its output and exit code). Say in
the body which you ran.

## Rehearsal

`$0` set (`/backlot-debloat hubspot`, or `/backlot-debloat hubspot tests` to fix the kind) means
rehearsal on that area. You read GitHub freely and write to it never: no issue, label, comment,
push or pull request. Skip the survey's choice of area, but find the closed files (step 1) first and
leave them alone as a live run would, so the rehearsal makes only cuts a live run could; list them
with the pull requests that close them in your summary. If every file of the area is closed, or
every file of the kind named, say so and stop before step 2.

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
change and the reviewers (step 8) after any fix, and that is the run. So is one with a maintainer's
comment you have not answered; a reply to your decision comment means what its option said —
`merge` is `gh pr ready` and step 9, `drop` is undoing the disputed cut and proving again from
step 5, `close` is `gh pr close` with a one-line comment. If two debloat pull requests
are already open, do nothing else: a maintainer has enough to read.

Otherwise pick the area. A file is closed while a pull request in review — open, not a draft, not
labelled `hold` — touches it, since a cut there would conflict with work a maintainer is about to
merge; the run leaves closed files alone and works the rest of its area. Each closed file, with the
pull requests that close it:

```bash
gh pr list -R brekkylab/backlot --state open --json number,isDraft,labels,files --limit 100 \
  --jq '[.[] | select(.isDraft | not) | select([.labels[].name] | index("hold") | not) | {n: .number, f: .files[].path}] | group_by(.f)[] | "\(first.f) \(map(.n))"'
```

Pipe it through `grep` for an area's paths to see which of its files are closed.

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

Unless the kind is known to be `prose`, record the base now, in the background, while you read,
from a worktree with its own environment and the script from your branch; record it a second time
unless the kind is known to be `tests`, since a `code` cut needs the second as calibration. Each
recording takes two to three minutes.

```bash
git worktree add --detach $REC/tree $BASE && cd $REC/tree && uv sync --all-extras -q \
  && uv pip install -q --no-deps "llama-index-readers-hubspot<0.6" && {
  uv run python <your working tree>/scripts/invariance.py record $REC/base
  uv run python <your working tree>/scripts/invariance.py record $REC/base2
}
```

The reader install matches what `scripts/cloud_session_start.sh` puts into your working tree, so the
two recordings run over the same packages; a test that skips on one side and runs on the other is a
difference no cut made.

`record` exits non-zero when any test fails, so the two are not chained with `&&`. A test that fails
at the base fails at the head too and the proof still compares; name it in the summary, and file it
if no issue has it.

Creating the branch at the commit you are on and returning to the starting branch at the end are the
only checkouts in the working tree you run in; never check out another commit there, since the
skill and the reviewers are read from it.

## 3. Read the area and choose the cut

What to read depends on the kind. `code`: the area's code files whole, and of its test files the
private names they import or reach into (`grep -nE "from backlot\.[a-z_.]+ import|[a-z_]+\._[a-z]"
<files>`), which a `code` cut must keep. `tests`: the test files whole. `prose`: the passages the
`verbose` list below names, longest first, and what each sits over; the prose of both columns of the
area is in scope. Then list what the rules allow, by kind:

Start with two lists. `uv run python scripts/invariance.py verbose <the area's files>` gives every
comment and docstring block of six lines or more, longest first, with how many lines of code a
docstring sits over; a docstring longer than its code, and a module docstring that summarises the
docstrings below it, are the first places to rewrite. `uv run python scripts/invariance.py similar
<the area's files>` lists every function in them, other than a `test_` function or one under three
lines, whose body is at least 80% alike to one anywhere in `backlot/` or `tests/`; a pair across two
vendors is rule 3's to judge, not a cut by default. Both read Python only: the `docs` area's files
are read whole. Then:

- **code**: functions and blocks repeated within the area or with a neutral module; helpers in two
  places doing the same thing; names nothing reads (rule 6); constants and tables spelled twice; and
  every rewrite rule 5 allows, function by function.
- **prose**: every passage rule 5 would shorten, longest first; then the same fact in two comments,
  a doc paragraph that a generated table or another doc already carries, a comment narrating history
  (rule 8). For each paragraph, grep its rarest three or four words across `*.md` and `backlot/`; a
  second hit is a candidate.
- **tests**: tests whose assertions are a subset of another's, parametrized cases that land in the
  same branch with the same answer shape, setup rebuilt test after test, helpers repeated across
  test files, and the rewrites rule 5 allows.

For `code` and `tests`, when the base recording is done and the area has code paths, run `uv run
python scripts/invariance.py uncovered $REC/base <the area's code paths>`, and make no `code` cut
before it has: a cut in a line it lists is undone, and undoing costs more than waiting. A `code` cut
stays off every line it lists (rule 2). Each listed line is also residue for step 7: reachable code
no test sends the request for, or a test that claims more than it reaches.

A test candidate is settled by the step-5 comparison, not by reading alone: a line only it executed
shows up there as lost coverage, and that test is not a *subset*. A parametrized case that is the
only one sending its value of a parameter is the usual way an *equivalent case* turns out not to be.

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
whole suite passes (`uv run pytest`), as AGENTS.md asks; for `code` and `tests` the head recording
is that run.

## 5. Prove

For `code` and `tests`, record the head from your working tree:
`uv run python scripts/invariance.py record $REC/head`. `record` refuses a directory that is not
empty, so a second head recording after a fix goes to a new name (`$REC/head2`). Then by kind:

- `prose`: `uv run python scripts/invariance.py prose $BASE`, and `uv run pytest tests/test_docs.py
  tests/test_skills.py`. Neither sees a fact disappear from prose, and for a markdown-only cut
  `prose` prints `invariant` over zero files, so the fact map is the proof. For a passage removed as
  a duplicate: where the same fact is still stated (file and line at the head). For a passage
  rewritten: each measured fact and constraint it stated, and that the new text still states it;
  then what was dropped, by rule 5's categories (motivation, history, restates the code, …). A
  measured fact with no row is a fact lost: put it back. Cite where a fact now lives by function or
  section name rather than by line, so a fix round does not renumber the map.
- `code`: `uv run python scripts/invariance.py compare $REC/base $REC/head --mode code
  --calibration $REC/base2`.
- `tests`: `uv run python scripts/invariance.py compare $REC/base $REC/head --mode tests`.

It must print `invariant`. A test whose outcome alone differs, with no response differing, may be a
timing test failing under load: record the head once more (`$REC/head2`) before blaming a cut. Any
other difference is a behaviour the cut changed: find which candidate, undo it, and prove again.
Never widen the comparison to make it pass. Keep `$REC` until the reviewers pass, since a fix is
proved against the same base recording, then `git worktree remove --force $REC/tree` and, in a live
run, `rm -rf $REC`.

For `tests`, also write the fact map: for every test removed or merged, the reason (rule 7) and the
test and assertion that now carry each fact it asserted. Coverage cannot see a lost assertion, so
prove each row: make a one-line change to `backlot/` that the removed test fails on at the base,
then run the carrier's file at the head (and the whole suite if that passes). Something must fail.
`uv run python scripts/invariance.py mutate <file> '<old>' '<new>' -- <test files>` makes the
change, runs the tests, restores the file and prints `caught`, `missed` or `no verdict` (nothing
selected, or a test errored — a mutation that breaks an import proves nothing); run it in the base
worktree for the removed test and in yours for the carrier, never while a recording runs. A row
whose carrier does not print `caught` is a fact you were about to lose: put the test back, or fold
its assertion into the carrier.

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
  the fact map, one row per passage.
- **Verification** — the three lines the template asks for, plus the measurement table of step 6;
  each count appears once, in the line or in the table.
- In a rehearsal, an issue you would have filed is cited by its title in parentheses.
- The body names the recordings `base`, `base2` and `head` whatever their directories are called:
  how many head recordings it took is the work, not the result. Fix commits from review rounds
  stay as they are; the maintainer squashes on merge.
- Checkboxes: tick the third, which speaks for the lines this pull request writes. Leave the first
  two unticked with `— not applicable: behaviour is unchanged` after each.

Dispatch both reviewers in parallel with the Agent tool, in the foreground so their verdicts come
back to you, each with the one-line prompt `Review pull request #<n> of brekkylab/backlot`
(rehearsal: `Review branch <branch> against <base>; run git from <your working tree>, read-only`,
followed by the title and the path of a file holding the body you would have sent). From the
second round on, add `The fact map passed at <sha>; check only the passages changed since.` when
the invariance reviewer's last verdict found nothing in the map.

- `subagent_type: invariance-reviewer`
- `subagent_type: prose-reviewer`

If the Agent tool's list of agent types does not include `invariance-reviewer` (a session started
before the file existed does not), dispatch `subagent_type: general-purpose` with the prompt `Your
instructions are .claude/agents/invariance-reviewer.md; follow them as your system prompt.` followed
by the line above.

Treat findings as the loop's step 9 does: reproduce, then fix or rebut with evidence, prove again
after any fix, and dispatch again in fresh contexts until both pass on the same commit. A finding
that a cut changed behaviour or lost a fact is answered by undoing or restoring it, never by arguing
it is harmless. After three rounds with a `block` standing: if every finding of the last round was
applied undisputed, dispatch that reviewer once more; otherwise, or if it still blocks, draft the
pull request, add `needs-maintainer`, and post a decision comment (see the top of this skill) whose
options are `merge`, `drop <the disputed cut>` and `close`.

## 9. Hand over

When both pass and `gh pr checks <n> --watch --fail-fast` is green, add `ready-for-maintainer` and
request review from `brekkylab/backlot-reviewer`. Then turn on the platform's auto-fix for the pull
request, so this session is woken when a maintainer reviews it or a check fails; if the session has
no such ability, say so in the summary, and the next scheduled run picks the review up in step 1.
End with one paragraph: the area and kind, what was cut in lines and tests, the proof's result, the
issues filed, the pull request.

## After hand-over: being woken

A wake-up is about this pull request alone. React 🚀 to it, then work every unanswered review
comment and review body as step 1 says: reproduce, fix and prove again, or answer with the
evidence that contradicts it, and run the reviewers again on what changed. A comment asking for a
choice the rules leave open is answered with a decision comment and `needs-maintainer`, never with a
question in the session. An approval, a merge or a close is the end: change nothing and stop.

## Budget

One run, one pull request. If the survey or step 3 finds nothing the rules allow, say so in one line
and stop.
