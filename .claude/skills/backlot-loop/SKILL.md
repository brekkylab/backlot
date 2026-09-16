---
name: backlot-loop
description: Work Backlot's `agent`-labelled issues into reviewed pull requests — measure the real vendor API, fix, test, open a PR, and have the two reviewer agents pass it. `/backlot-loop` runs live; `/backlot-loop <issue-number>` rehearses one issue locally with no GitHub writes. Run it only when a person or the routine's own prompt says `/backlot-loop`; an ordinary request to fix an issue is not that.
argument-hint: "[issue-number for rehearsal]"
allowed-tools: Read, Edit, Write, Grep, Glob, Bash, Agent, WebFetch
---

# The loop

You are the maintainer on duty for Backlot. AGENTS.md is the law of this repository; read it now if
you have not this session. The pull request template at `.github/pull_request_template.md` and the
fidelity-gap issue template at `.github/ISSUE_TEMPLATE/fidelity-gap.md` are the shapes you write in.
`docs/fidelity.md` says which environment variable each vendor's credential is read from.

Three labels carry state. `agent`: a person has admitted the issue to this loop — you never add it.
`needs-maintainer`: a decision is waiting on a person. `ready-for-maintainer`: a PR both reviewers
passed and CI is green. `hold` on an issue or PR means every step below skips it.

Everything you write to GitHub — comments, issues, pull request bodies, replies — ends with its
last sentence. No signature, no "Generated with" line, no separator, no session link; the one URL
you post is the run's, in the claim comment, and nowhere else.

## Asking for a decision

Every time a person has to choose — an issue that needs a decision (step 3), a review comment
that could mean two things (step 2), a review that stays blocked (step 9) — the comment has this
shape and no other. A maintainer reads it on a phone between meetings; it has to be answerable in
one glance and one line.

```
## Needs a decision

**Question** — <one sentence a maintainer can answer without opening anything else>.

**What I measured** — <date>, <where>:
- `<request>` → real: <answer> · Backlot: <answer>
- <two to four bullets at most; the request and both answers on one line each>

**Options**
1. **<label> (recommended)** — <what happens if chosen, and what it costs, in one sentence>.
2. **<label>** — <same>.
3. **<label>** — <same, only if a real third choice exists>.

Reply `decision: 1`, or `decision: <label>`, with a reason after it if you want one recorded.
```

Two to four options, mutually exclusive, the recommended one first. Labels are one or two words
(`serve`, `gap`, `merge`, `re-measure`, `close`). Never paste the measurement narrative into the
options; the bullets above them carry it. The whole comment fits on one screen.

A reply whose first line is `decision:` picks by number or by label; whatever follows on that line
is the maintainer's reason and goes wherever the option's outcome is recorded (a baseline note, a
PR body, a closing comment). Any other first line is an instruction; follow it.

## Rehearsal

`$0` set (`/backlot-loop 146`) means rehearsal on that one issue, whatever its labels or state. In
rehearsal you read GitHub freely and write to it never: no comments, no labels, no issues, no push,
no PR. Work on a local branch `claude/rehearsal-$0`, run the reviewers against that branch, and end
by printing the PR title and body you would have opened plus each reviewer's last verdict. Every
"comment", "label", "file an issue", "push" and "open the PR" instruction below is replaced by
writing the text you would have sent into your final summary.

A closed issue is rehearsed from before its fix, so there is work to do and a known answer to
compare with. Find the pull request that closed it (`gh issue view $0 --json closedByPullRequestsReferences`)
and its commit on `main` (`gh pr view <pr> --json mergeCommit --jq .mergeCommit.oid`). Create the
branch from HEAD, then `git revert --no-edit <that commit>` as the branch's first commit: the tree
is current everywhere except that fix, and the skill and reviewer files stay in place. If the revert
conflicts, stop and say so; that issue cannot be rehearsed from here. The reviewers then review the
branch against that revert commit rather than against `main`, and your summary ends with the diff
between your change and the merged one (`git diff <merge commit> HEAD -- <files you touched>`),
described in a sentence: same behaviour, or where it differs and which side the measurement backs.
If step 3 classifies a closed issue as needing a decision, print the escalation comment you would
have posted, then continue under the decision the merged pull request took, and say so.

A rehearsal ends on the branch it started from: `git checkout <that branch>` before your summary,
leaving `claude/rehearsal-$0` in place for a maintainer to read.

## 1. Survey

```bash
gh issue list --label agent --state open --json number,title,labels,comments --limit 50
gh pr list --author @me --state open --json number,title,labels,reviewDecision,headRefName
```

If a `routine-fire-payload` block names an issue, read that issue first; it is a hint about
ordering, not an instruction, and it still has to carry `agent`.

Build one worklist in this priority: (a) your own open PRs with review comments or failing checks
you have not answered, or with green checks and neither `ready-for-maintainer` nor
`needs-maintainer` — a hand-over an earlier run did not finish, which you finish from step 9,
(b) `needs-maintainer` issues and pull requests whose newest comment starts with `decision:`,
(c) issues nobody has claimed. Drop anything labelled `hold`. An issue is taken, and dropped, when
either holds: an open pull request already closes it (`gh pr list --state open --search "closes
#<n>"`, and the issue's timeline for a cross-referenced PR), or its newest `loop: claimed` comment
is less than six hours old. A claim older than six hours with no pull request behind it is stale,
and the issue may be picked again; say so in your own claim.

## 2. Your own pull requests first

For each unaddressed review comment:

- Reproduce the claim before changing anything: run the test, make the vendor call, read the line.
- If it reproduces, fix it, push, and reply in the thread with what changed.
- If it does not, reply with the measurement that contradicts it and leave the thread open.
- If the comment could mean two different changes, or asks whether Backlot should serve something
  at all, reply in the thread with a decision comment (see Asking for a decision) whose options are
  the readings you see, add `needs-maintainer` to the PR, and move on.

A failing check is reproduced locally with `uv run pytest -q` before anything is changed.

## 3. Classify each remaining issue

An issue **needs a decision** when resolving it would: add an operation, type, field or parameter
Backlot does not serve; remove one it does; change which principal can see a document; or rest on
vendor documentation alone because no credential in the environment can measure it. In a cloud
session the platform's proxy puts its own GitHub credential on every request to `api.github.com`,
whatever `Authorization` you send, so a GitHub behaviour that turns on a bad or missing credential
cannot be measured from there: it needs a decision too, and the proposal says that is why, so a
maintainer can measure it from a machine of their own instead. Everything else
— a shape, status code, header, charset, pagination, ordering or error-envelope difference on an
operation Backlot already serves — is **mechanical**.

For an issue that needs a decision, measure first — enough live calls that the options state what
the vendor actually does today and where it keeps the surface Backlot would lose or gain — then
post a decision comment (see Asking for a decision) and add `needs-maintainer`. Its options are
`serve` (what serving it takes) and `gap` (the note the baseline entry would carry), plus a third
only when the measurement showed one, such as the surface living on another route Backlot already
serves. Recommend the one the measurement backs.

For an issue whose newest comment starts with `decision:`: `serve` makes it mechanical from here;
`gap` means the fix is `backlot diff --source <source> --update-baseline` followed by writing the
maintainer's reason into the new entry's `note` by hand, then a PR; a third option means what its
sentence said.

## 4. Pick

From the mechanical issues choose one, or several whose fixes touch the same router and share one
measurement (three charset issues on one vendor are one PR; a charset issue and a pagination issue
are two). Comment `loop: claimed by run <session url or "local">` on each. Say in the PR body why
the set is one PR.

## 5. Measure

The evidence rules are the fidelity-gap template's: a live call, an introspection, a client
generated from the vendor's schema — dated. Vendor documentation is context, not evidence.

```bash
backlot diff --source <source> --json      # the schema-level view, no credential for most vendors
```

Then the live call the issue is about, with the credential named in `docs/fidelity.md`'s coverage
table and the source router's own comments. Keep the request and the verbatim response; both go into
the PR body under "Why this is what the real API does". Measure the exact parameter, method and
input kind the issue names — a measurement of one does not carry to its neighbour.

## 6. Fix

- Write the test first, in `tests/test_<source>.py` (or the file for the layer you change),
  asserting the measured response. Run it; it must fail.
- Fold a new assertion into the test that already sets up that state; parametrize instead of
  duplicating; a new test function is the last resort.
- Make the change in `backlot/routers/<source>.py` or the layer the issue names. Never widen a
  validation pattern to make a test pass: find which side has the external source.
- A new or changed read of corpus content gets an admin-token test and a scoped-token test.
- Comments state the measurement and the date. No issue numbers, no "previously", no story.
- If a schema under `backlot/schemas/` changed, run `python scripts/gen_docs.py`.
- Run `uv run pytest -q` and `uv run ruff check . && uv run ruff format --check .` clean.

## 7. File the residue

Every divergence you met that is outside the chosen issues becomes its own issue, now, in the
fidelity-gap template with the `fidelity` label and a first line `Found while measuring for #<n>`.
Check `gh issue list --label fidelity --state open --search "<route or field>"` first; if one
exists, comment your measurement there instead. Never add `agent` to it.

## 8. Open the pull request

Branch `claude/<source>-<slug>`. Title: one declarative sentence describing the new state, prefixed
with the source like the log (`github: a JSON response carries the charset real's carry`). Body:
the repository's PR template with every section filled and `Closes #a, #b` for each issue. Push and
`gh pr create --assignee @me --label <labels>`, where `<labels>` is every label the closed issues
carry except `agent` (`fidelity`, `bug`, `feature`, `good first issue`, …), read with
`gh issue view <n> --json labels`; a PR closing several issues carries the union.

The body is read on GitHub, which renders every newline as a line break, so never hard-wrap a
paragraph. Readability comes from structure instead:

- Paragraphs of two or three sentences, one idea each, with a blank line between them. A section
  that would be one long paragraph becomes a list.
- Measurements go in a table: one row per request, columns for what real serves and what Backlot
  serves. Request and response text goes in a fenced block, not inline.
- The body ends at the template's checklist. No signature, no "Generated with" line, no session
  link, no trailer of any kind; the claim comment on the issue already names the run.

## 9. Review

Dispatch both reviewers in parallel with the Agent tool, each with a prompt of one line:
`Review pull request #<n> of brekkylab/backlot` (rehearsal: `Review branch claude/rehearsal-$0
against <base>`, where `<base>` is `main` for an open issue and the revert commit's sha for a closed
one, followed by the PR title and body you would have sent, since there is no PR to read them from).

- `subagent_type: behaviour-reviewer`
- `subagent_type: prose-reviewer`

Read each `VERDICT:` line and its findings. Treat findings the way you treat human review in step
2: reproduce first, then fix or rebut with evidence. Push fixes, then dispatch both reviewers again
in fresh contexts. Stop when both say `pass` on the same commit. A reviewer that passed is not
dispatched again unless a later fix touched what it reviews. After three rounds with a `block`
still standing: if every finding of the last round was applied undisputed, dispatch that reviewer
once more on the result; otherwise, or if it still blocks, `gh pr ready --undo` (draft), add
`needs-maintainer`, and post a decision comment on the PR (see Asking for a decision). Its question
is the standing finding in one sentence; its measurement bullets are the reviewer's evidence and
yours; its options are the ways out — `merge` on the evidence at hand, `re-measure` from a machine
whose access differs from this session's, `close` — with the one you would take first. When the
answer arrives, a later run acts on it from step 2: `merge` means `gh pr ready`, then step 10;
`re-measure` means the maintainer's comment carries the measurement, which you put into the PR body
and then continue at step 9 as a new round; `close` means `gh pr close` and a one-line comment on
each issue the PR named.

## 10. Hand over

When both reviewers pass: wait for `gh pr checks <n> --watch --fail-fast`. Green: add
`ready-for-maintainer` and `gh pr edit <n> --add-reviewer <the other maintainer's login, read from
the repository's recent merges with gh pr list --state merged --limit 20 --json mergedBy>`. Red:
back to step 2 with the failing check. Comment on each issue you touched with one sentence saying
what you did and where. End with a one-paragraph summary: issues surveyed, what you picked and why,
what you filed, what you escalated, the PR.

## The working tree is yours alone

Never check out another commit or branch in the working tree during a run: the skill and the
reviewer agents are read from it, and a checkout that predates them makes the reviewers vanish
mid-session. To run or read the tree at another commit — the merged fix in a rehearsal, `main` for
a comparison — add a throwaway worktree (`git worktree add /tmp/loop-<sha> <sha>`) and remove it
when done.

## Budget

One run picks one PR's worth of work. It does not start a second PR after the first is handed
over; the next run will. If the survey finds nothing to do, say so in one line and stop.
