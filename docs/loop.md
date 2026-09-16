# The agent loop

[← README](../README.md)

A Claude Code routine reads the issues a maintainer has labelled `agent`, measures the real vendor
API, fixes Backlot, and opens a pull request that two reviewer agents have passed. A person does two
things: answers a judgment call when the loop asks, and merges. The procedure the routine follows is
[`.claude/skills/backlot-loop/SKILL.md`](../.claude/skills/backlot-loop/SKILL.md); the reviewers are
[`behaviour-reviewer`](../.claude/agents/behaviour-reviewer.md) and
[`prose-reviewer`](../.claude/agents/prose-reviewer.md). Changing how the loop works is a pull
request against those files.

## Driving it

| You want | You do |
|---|---|
| the loop to work an issue | add the `agent` label. Nothing else admits an issue, including the nightly fidelity report and the issues the loop files itself |
| to answer an escalation | reply on the issue with a first line `decision: serve` or `decision: gap <why>`. `serve` sends it down the fix path; `gap` has the loop write the baseline entry with your reason as its note |
| to stop one item | add `hold` to the issue or PR; every run skips it |
| to stop everything | pause the schedule on the routine's page |
| to merge | a PR labelled `ready-for-maintainer` has both reviewers' pass and green CI; review it as you would any other and merge |

Labels the loop sets: `needs-maintainer` (a decision is waiting; the proposal is the last comment)
and `ready-for-maintainer`. It never sets `agent`.

Adding `agent` or posting a `decision:` comment also rings the routine through
[`loop-doorbell.yml`](../.github/workflows/loop-doorbell.yml), so the run starts within minutes
rather than at the next scheduled slot.

## Reading a run

The routine's page on claude.ai lists runs. A green run means the session exited without an
infrastructure error, not that it did anything useful; the record of what a run did is the comment
it leaves on each issue it touched and the summary at the end of its session. A run that found
nothing labelled `agent` says so and stops.

## What the routine is

Recorded here so it can be recreated on another account in a morning.

**Prompt**, model set to the strongest in the selector:

> Run `/backlot-loop`. In short: look at the open issues labelled `agent` and at the open pull requests you
> opened earlier. Address review comments on your own pull requests first. Skip anything labelled
> `hold`. For an issue that needs a decision Backlot's maintainers have not made, comment your
> proposal and label it `needs-maintainer`; if it already carries a `decision:` comment, follow that
> decision. From the rest, pick one issue or a set of related ones, measure the real vendor API with
> the credentials in the environment, fix Backlot, and open a pull request that closes them.
> Anything you found that is outside that scope becomes a new issue, not part of the pull request.
> If a `routine-fire-payload` block names an issue, look at that one first. The skill has the full
> procedure; follow it.

**Repository**: this one. **Schedule**: every two hours, 09:00–21:00 Asia/Seoul, weekdays. **API
trigger**: on; its URL and token live only in this repository's `LOOP_FIRE_URL` and
`LOOP_FIRE_TOKEN` secrets. **Connectors**: none.

**Environment** `backlot-loop`, personal to the loop account. Network access **Custom** with the
default registries kept, plus: `slack.com`, `*.atlassian.net`, `api.atlassian.com`,
`*.googleapis.com`, `oauth2.googleapis.com`, `api.hubapi.com`, `api.linear.app`,
`api.fireflies.ai`, `api.notion.com`, `*.amazonaws.com`. Setup script: `uv sync --all-extras`.

Environment variables are the vendor credentials [`docs/fidelity.md`](fidelity.md) and the source
routers read: `SLACK_USER_TOKEN`, `HUBSPOT_API_KEY`, `HUBSPOT_PERSONAL_ACCESS_KEY`,
`LINEAR_API_KEY`, `FIREFLIES_API_KEY`, `ATLASSIAN_ORG_ID`, `ATLASSIAN_ORG_API_KEY`,
`ATLASSIAN_USER_EMAIL`, `ATLASSIAN_USER_API_TOKEN`, `GOOGLE_CLIENT_ID`, `GOOGLE_CLIENT_SECRET`,
`GOOGLE_REFRESH_TOKEN`, `AWS_ACCESS_KEY_ID`, `AWS_SECRET_ACCESS_KEY`, `AWS_DEFAULT_REGION`,
`AWS_S3_BUCKET`, `NOTION_API_KEY`. **Never `GITHUB_TOKEN` or `GH_TOKEN`**: either one replaces the
platform's GitHub credential with its literal value, and every `gh` call in the run fails. Anyone
whose sessions use this environment can read its variables, which is why it is personal to the loop
account and shared with nobody.

## Cost and identity

A run is subscription usage on the loop account, and runs count against that account's daily
routine cap, both shown on the routine's page. Parallel runs share the account's rate limit.
Everything the loop does on GitHub — commits, pull requests, comments — appears as the loop
account's GitHub user, so the other maintainer merges.

## Rehearsing a change to the loop

`/backlot-loop <issue-number>` in a local checkout with the vendor credentials in the environment runs the
whole procedure on that issue without writing to GitHub: no comments, no labels, no push, no PR.
It ends by printing the pull request it would have opened and both reviewers' verdicts. Use it on a
closed issue whose merged fix you know before changing the skill or a reviewer.
