# Measuring fidelity

[← README](../README.md)

Fidelity to the real APIs is the point of this project, and a divergence is a bug. That policy
only catches what someone goes looking for. `backlot diff` runs the same measurement on a
schedule, so a vendor changing its schema in March is noticed in March.

## What it compares

Backlot's own schema against the vendor's own schema, **in both directions**, over arguments as
well as fields:

For a **GraphQL** source, the schemas themselves — field by field, argument by argument. Backlot's
side is the SDL the server builds its engine from; the vendor's side is a live introspection
response, which needs a credential.

For a source compared against a **document its vendor publishes**, the request surface — which
operations exist, and which query parameters each accepts. Backlot's side is the app's own
`/openapi.json`. The vendor's side comes in two formats, read by two parsers, because Google does
not publish OpenAPI: an **OpenAPI** document for GitHub, Slack, Jira, Confluence, Notion and
HubSpot, and a **Google API Discovery** document for Gmail and Drive. Both are public, so these
comparisons run with **no credential, no quota and no account**. Response bodies are out of scope
here: a vendor spec describes them through deep `$ref` chains that Backlot's `response_model` set
does not mirror shape-for-shape, so a body diff would report how two documents are written rather
than how two servers answer.

Path templates are compared with placeholders flattened. The vendor calling a segment `{userId}`
and Backlot calling it `{user_id}` is not a divergence.

A one-direction, fields-only comparison is worse than none: run against Linear once, it reported a
clean schema while ten fields and four arguments were missing, because every one of them was on the
side it did not walk.

```bash
backlot diff --source slack                        # no credential: a published document is public
backlot diff --source fireflies                    # reads FIREFLIES_API_KEY from the environment
backlot diff --source fireflies --credential api_key=…   # or pass it, see the caveat below
backlot diff --source fireflies --update-baseline  # accept what it found
backlot diff --source fireflies --json             # the same result, machine-readable
```

Output is coloured and wrapped for a terminal — bold for identity, colour for severity, dim
for the supporting sentence — and the codes are dropped when anything else is reading, so a
redirected run or a CI log stays plain text.

`--json` prints one object on stdout and nothing else there, so it pipes:

```bash
backlot diff --source linear --json | jq '.new[] | select(.severity == "breaking") | .path'
```

It carries `source`, `endpoint`, `total`, `new` and `resolved` — or, with `--update-baseline`,
`acknowledged`, `unacknowledged` and the `baseline` path. Exit codes are the same either way.

Each source **declares** the credentials it needs, by a logical name and the environment variable
it is read from, and `--credential NAME=VALUE` repeats for as many as a source declares. A single
`--token` would not stretch: a vendor authenticated with SigV4 needs an access key *and* a secret,
and a Google service account needs a client id, a client secret and a private key. Declaring them
also means a name a source does not take is **refused** rather than ignored — nine of the eleven
sources need no credential at all, and that is exactly where a silently accepted option goes
unnoticed.

Prefer the environment. A value passed on the command line is visible to any process that can run
`ps`, and it lands in shell history.

Exit codes are distinct on purpose: `1` is "the schemas disagree", `2` is "the vendor's schema
could not be read". A vendor outage is not a fidelity finding.

## A published spec is weaker evidence than introspection

Introspection *is* the contract: a field absent from it is absent from the API. A published OpenAPI
document only *describes* the contract, and it lags, omits paid tiers, and documents one verb where
the server accepts two. Every `extra_operation` this has reported so far was the spec being
incomplete rather than Backlot being wrong:

- Slack's spec documents one verb per method and omits the search methods entirely. Measured against
  slack.com, all fourteen answer `200/ok` over **both** GET and POST.
- GitHub's spec describes neither `contents` without a path nor the legacy per-sha statuses read.
  Measured against api.github.com, both answer `200`.
- Atlassian's published Confluence v1 document no longer describes **any** read at all: it retains
  the write operations and `search`, and nothing else. The v1 reads are deprecated in favour of
  Confluence REST v2, not documented as removed — so on that source this comparison currently
  covers almost nothing, which the baseline notes say plainly.

So on a REST source, read `missing_*` as reliable and `extra_*` as *undocumented by the vendor —
verify by hand*, never as proof of a bug. Those measurements are what the baseline notes carry.

## Severities

`breaking`
: Backlot contradicts the vendor — a field or argument the vendor does not have, or the same name
at a different type. Code written against Backlot compiles and then behaves differently against
the real service, which is the failure this project exists to prevent. Always a bug.

`gap`
: The vendor has surface Backlot does not. Backlot serves a deliberate subset, so most of these are
scope. A gap on a type Backlot *does* serve is worth reading: it is where a real client's query
fails against Backlot.

Types the vendor declares and Backlot never mentions are not reported one per type — the ninety-odd
of them would bury everything else. They surface where they matter, as a `missing_field` on a type
Backlot does serve.

## S3 is asked, not read

S3 dispatches on the query string, not the path: `GET /{Bucket}` is ListObjects,
`?list-type=2` is ListObjectsV2, `?location` is GetBucketLocation, and ninety more operations sit
at the same path. Backlot serves them from four catch-all routes that declare no query parameters
and read the string themselves, so a path-and-parameter diff pairs every S3 operation with the same
route and reports a clean match every time — a green check that means nothing.

So S3 is compared by asking a running server instead. Backlot starts on a free port, every read
operation botocore declares is sent to it signed, and the answer is classified:

- **refused** — the honest answer for an operation Backlot does not serve. A `gap`.
- **answered distinctly** — implemented. No finding.
- **answered with the body the same path returns when nothing selects an operation** — Backlot
  neither implements the operation nor refuses it, so the caller parses another operation's body
  under a 200, with no error and no log line. `silent_fallthrough`, and breaking.

Requests are signed with [`backlot.sigv4`](../backlot/sigv4.py) — the module that verifies them —
so the probe adds no dependency and a change to signing breaks both sides at once.

## The baseline

`backlot/fidelity/baseline/<source>.json` holds the divergences already read and accepted, so a run
reports what is **new**. Without it the first Fireflies run lists fourteen root fields Backlot never
claimed to serve, and the first Linear run lists seven hundred and eighty-two — by the third run
nobody reads the output. The baselines ship inside the package, so an installed copy can be compared
against its vendor without the repository.

Accepting a divergence is a file change, so it goes through review like any other. Each entry
carries a note saying why the gap is deliberate — which makes this file the written record of what
Backlot does and does not claim about a vendor:

```json
{
  "kind": "missing_field",
  "severity": "gap",
  "path": "Query.active_meetings",
  "detail": "vendor serves active_meetings: [ActiveMeeting!]; Backlot does not",
  "note": "Meetings in progress right now. Backlot serves a fixed corpus, so there is no live state to report."
}
```

`--update-baseline` acknowledges gaps freely and **never acknowledges a breaking finding**, which
is a bug by this project's own rule rather than a gap. It still writes the gaps — leaving them out
would bury the breaking ones under hundreds of repeats every run, which is the same silence by
another route — and exits 1 naming what it left live.

There is no flag that silences one. Acknowledging a breaking divergence is a hand edit to the
baseline file, carrying a note that says why the vendor's shape is not being matched, so what a
reviewer sees in the diff is the reasoning rather than a flag on a command nobody kept. An entry
added that way survives later rewrites: `--update-baseline` rewrites the file, it does not
re-litigate it.

It stops covering a shape that moves, though. A `gap` is acknowledged by what it names — the vendor
has surface Backlot does not, and the vendor restating it at a new type does not change what was
accepted. A `breaking` entry is acknowledged by what it names **and** by its `detail`, because
there the detail *is* the contradiction: an entry reading `vendor: Int, Backlot: Float` says
nothing about a vendor now serving `String`, and going on silencing it is exactly the March drift
this command exists to catch. Such an entry is reported again, and left in the file exactly as
written — the note is the record of the reasoning, and a rewrite does not delete it — until someone
reads the new shape and rewrites it by hand.

A run also reports **resolved** entries: an acknowledged divergence the vendor no longer has. The
baseline has gone stale, which is its own kind of drift.

## In CI

[`.github/workflows/fidelity.yml`](../.github/workflows/fidelity.yml) runs every source daily,
and on demand through `workflow_dispatch`. Never on a pull request: drift is this project's bug,
but it is never the bug of whichever pull request happens to be open when a vendor ships a change,
and a contributor fixing a typo must not be blocked by it. A new divergence opens an issue and
turns the scheduled run red instead.

## Coverage

Sources are named the way the rest of Backlot names them — the `source_type` a BYO record carries,
which is also what `backlot/schemas/` defines. Fidelity keeps no source list of its own; it says how
each of those is compared, and a test fails if the two sets ever drift apart.

| Source | Compared through | Credential |
|---|---|---|
| Fireflies | GraphQL introspection | `api_key` — `FIREFLIES_API_KEY`, sent as `Bearer <key>` |
| Linear | GraphQL introspection | `api_key` — `LINEAR_API_KEY`, sent **bare** |
| Slack | published OpenAPI | none |
| Gmail | Google Discovery | none |
| Google Drive (`google_drive`) | Google Discovery | none |
| GitHub | published OpenAPI | none |
| Jira | published OpenAPI (v3) | none |
| Confluence | published OpenAPI (v1) | none |
| HubSpot | published OpenAPI, resolved through the API catalog | none |
| Notion | published OpenAPI | none |
| Amazon S3 | botocore service model, **probed** — see below | none |

The credential column is measured, not read off a page: Linear's personal API keys go in bare, and
sending Linear a `Bearer` prefix is answered **400**, not 401.

**Jira's `/rest/api/2` aliases** are served because real Jira serves them, but Atlassian publishes a
v3 document. They are compared through their v3 twins rather than reported as invented.

Two vendors need a second document before they are fully covered: HubSpot's v4 associations surface
is a separate API in the same catalog, and Confluence's reads now live in a v2 document.
