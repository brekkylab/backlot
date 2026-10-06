# Supported sources

[← README](../README.md)

Every source Backlot serves and every endpoint of each.

Endpoints are written relative to their service's prefix, in the spelling the vendor's own docs use.
Everything is `GET` unless a row says otherwise.

## Every source Backlot serves

Generated from `backlot/schemas/*.schema.json` and the app's own `/openapi.json` by
`scripts/gen_docs.py`. Do not edit this table by hand — run the script.

<!-- generated:sources start -->
| `source_type` | Service | URL prefix | Endpoints | Record schema | What one record is |
|---|---|---|---|---|---|
| `confluence` | Confluence | `/atlassian/wiki/rest/api` | 9 | [`confluence.schema.json`](../backlot/schemas/confluence.schema.json) | A Confluence page or blogpost. |
| `fireflies` | Fireflies | `/fireflies/graphql` | GraphQL (one `POST`) | [`fireflies.schema.json`](../backlot/schemas/fireflies.schema.json) | A Fireflies.ai meeting transcript. |
| `github` | GitHub | `/github` | 34 | [`github.schema.json`](../backlot/schemas/github.schema.json) | A GitHub issue, pull request, file, or the repository itself. |
| `gmail` | Gmail | `/gmail/v1` | 8 | [`gmail.schema.json`](../backlot/schemas/gmail.schema.json) | A Gmail message. |
| `google_drive` | Google Drive, Docs, Sheets, Slides | `/drive/v3` `/docs/v1` `/sheets/v4` `/slides/v1` | 13 | [`google_drive.schema.json`](../backlot/schemas/google_drive.schema.json) | A Google Drive file. |
| `hubspot` | HubSpot | `/hubspot` | 5 | [`hubspot.schema.json`](../backlot/schemas/hubspot.schema.json) | A HubSpot CRM record (contact, company, deal, ticket, note, …). |
| `jira` | Jira | `/atlassian/rest/api` | 14 | [`jira.schema.json`](../backlot/schemas/jira.schema.json) | A Jira issue. |
| `linear` | Linear | `/linear/graphql` | GraphQL (one `POST`) | [`linear.schema.json`](../backlot/schemas/linear.schema.json) | A Linear issue. |
| `notion` | Notion | `/notion/v1` | 12 | [`notion.schema.json`](../backlot/schemas/notion.schema.json) | A Notion page or database. |
| `s3` | Amazon S3 | `/s3` | 4 | [`s3.schema.json`](../backlot/schemas/s3.schema.json) | An S3 object. |
| `slack` | Slack | `/slack/api` | 12 | [`slack.schema.json`](../backlot/schemas/slack.schema.json) | A Slack message. |
<!-- generated:sources end -->

## Per-service detail

Ordered as the table above, by `source_type`.

### Confluence — `/atlassian/wiki/rest/api`

| Endpoint | Notes |
|---|---|
| `content` | `spaceKey`, `title` (the whole title, ignoring ASCII case) |
| `content/{id}` | |
| `content/{id}/child/comment` | |
| `content/{id}/child/page` | |
| `content/{id}/label` | |
| `content/{id}/restriction/byOperation` | |
| `search` | CQL |
| `space` | `expand=description,permissions` |
| `space/{key}` | `expand=description,permissions` |

`expand=permissions` carries the space's permission roster on either read, one entry per ACL grant
— a user grant naming that user, a group or org grant naming none — for `read`/`space`, the only
operation an ACL states.

A JSON body is the bare `application/json` on every Confluence route served. The charset Jira names
is Jira's alone.

A `HEAD` is the `GET` with the body left off, and declares the length that body would have had on
every 200 but `search`'s. An `OPTIONS` is a 404 in the `errors` list the 405 uses, except on
`search`, which answers by `Accept` as JAX-RS does: its WADL for `*/*`, `application/xml` or no
`Accept` at all, and 204 for `application/json` or `text/html`, naming its three methods either way.
A path no route serves is JAX-RS's own 404 — JSON when the caller asks for `application/json` by
name, the `<status>` XML document otherwise — and a path below `space/` or `content/`, or anything
under `/wiki` outside the API mount, is the product's HTML 404 page instead; a caller with no
credential is sent from `/wiki/…` outside `/wiki/rest` to log in. An operation Confluence publishes
that no route here serves refuses a caller with no credential with a served route's 403, or from a
few services with `Current user not permitted to use Confluence`, and a caller whose credential
resolves gets the 404 an unserved path gets. Every answer Confluence itself gives carries
`atl-request-id`, `atl-traceid` (the same value without its dashes), `x-confluence-request-time`,
`x-content-type-options` and `x-xss-protection`, and what the content and space services' routes
answer adds the three headers that say the v1 REST API is deprecated.

### Fireflies — `/fireflies/graphql`

**GraphQL only**, one `POST`. Root `Query` fields:

| Query field | Notes |
|---|---|
| `transcripts` | The documented filters, below |
| `transcript(id:)` | One meeting, with its sentences |
| `user[(id:)]` | |
| `users` | |

Offset pagination — `limit` (**max 50**, clamped) / `skip` — returning a **bare list**, not a Relay
connection. The documented filters are `keyword` × `scope` (`title`\|`sentences`\|`all`),
`fromDate`/`toDate`, `host_email`, `organizers`, `participants`, `user_id`, `mine` and `channel_id`.

Field names are snake_case, as Fireflies' own schema has them. Full introspection.

### GitHub — `/github`

| Endpoint | Notes |
|---|---|
| `search/issues` | `q`: free text + `repo:` `is:` `state:` `type:` `label:` `author:` |
| `search/code` | `q`: free text over a file's body and path + `repo:` `path:` `filename:` `extension:` `in:file`/`in:path` |
| `orgs/{org}` | |
| `orgs/{org}/repos` | `type`, `sort`, `direction`: real's enums and defaults, each direction as measured; `created`, `updated` and `pushed` are one derived order here |
| `orgs/{org}/teams` | |
| `user/repos` | The token's own reach. `visibility`, `sort`, `direction`; `type` and `affiliation` select on what the caller is to a repository, which a corpus does not state, and stay undeclared |
| `rate_limit` | The windows the `x-ratelimit-*` headers report, `core`, `search` and `code_search`; a read of it does not count, and it answers with no credential at the anonymous limits, the one route here that does |
| `repos/{o}/{r}` | |
| `repos/{o}/{r}/issues[/{n}]` | `state` on the listing: `open`\|`closed`\|`all`; any other value is real's 422, and the repository is checked first, so an unknown repo is the 404 instead. `sort`, `direction`: real's enums and defaults, each order as measured |
| `repos/{o}/{r}/issues/{n}/comments` | |
| `repos/{o}/{r}/issues/comments/{id}` | |
| `repos/{o}/{r}/pulls[/{n}]` | `state` on the listing: `open`\|`closed`\|`all`; any other value is served as `open`, which is what real does here where the issue listing refuses it. `sort`, `direction` as on issues; `long-running` orders by creation and filters nothing, as it does on the wire |
| `repos/{o}/{r}/pulls/{n}/reviews` | |
| `repos/{o}/{r}/pulls/{n}/comments` | |
| `repos/{o}/{r}/pulls/{n}/files` | |
| `repos/{o}/{r}/pulls/{n}/commits` | |
| `repos/{o}/{r}/pulls/comments/{id}` | |
| `repos/{o}/{r}/readme` | |
| `repos/{o}/{r}/readme/{dir}` | That directory's README, and the repository's own for the empty directory a trailing slash sends |
| `repos/{o}/{r}/contents[/{path}]` | |
| `repos/{o}/{r}/git/trees/{ref}` | |
| `repos/{o}/{r}/git/blobs/{sha}` | |
| `repos/{o}/{r}/git/ref/{ref}` | Takes the ref as a trailing path, so a branch named `release/2026-03` resolves. `heads/`, `tags/` and `pull/{n}/head` or `/merge` only, and not the fully-qualified `refs/heads/…` — real 404s that here |
| `repos/{o}/{r}/branches[/{branch}]` | What a `subtype: "repo"` record states, else the default branch plus the refs the repo's pulls name; a name it omits is a 404. `protected`: selects, on the flag that record carries |
| `repos/{o}/{r}/branches/{branch}/protection` | Where a branch's `protection_url` points: real's 404 for a caller without repo-admin rights, which is every caller here |
| `repos/{o}/{r}/tags` | What a `subtype: "repo"` record states; empty for a repo that states none |
| `repos/{o}/{r}/commits/{sha}` | Takes a branch name too; a ref naming no commit is real's 422 |
| `repos/{o}/{r}/statuses/{sha}` | |
| `repos/{o}/{r}/collaborators` | |
| `repos/{o}/{r}/teams` | |

**`{o}` and `{r}` match in any case**, and every url in the answer names them as the corpus spells
them — as real does, which resolves `/repos/PSF/REQUESTS` to `psf/requests` rather than 404ing it.
A `repo:` search qualifier resolves the same way.

**Media types are honoured.** `Accept: application/vnd.github.raw` on `contents`/`readme`/`git/blobs`
returns the file's bytes; `…diff`/`…patch` on a pull returns a real unified diff / `git am` mbox; and
`…text-match+json` on `search/code` adds each hit's `text_matches` fragment. A JSON body is
`application/json; charset=utf-8`, as real's is on every route measured, except on `search/code`,
whose own 200 and 422 are the bare `application/json` real's code search backend sends; the 401
there is the gateway's answer rather than that backend's, and carries the charset.

**`X-GitHub-Api-Version` is honoured too**, in both values real currently supports: `2026-03-10`
drops `assignee` (issues and pulls) and `merge_commit_sha` (pulls), `2022-11-28` keeps them, an
unpinned request gets `2022-11-28`, anything else is the real API's 400, and every response reports
its choice in `X-GitHub-Api-Version-Selected`. `search/code` is the one route that does neither:
real's code search backend does not read the header, so a pinned version there is served whatever
it says and no response from it carries the `Selected` header (measured 2026-09-06).

**Every response carries the five `x-ratelimit-*` headers** real puts on every answer, the errors
included: `limit` at real's numbers (60 an hour for a caller with no credential, 5000 for a token,
30 and 10 for `search` and `code_search`), `remaining` and `used` counted per credential and per
resource, `reset` the second that window closes, `resource` the one the request counted against.
`core` measures an hour and the two search resources measure a minute, as real's do. A window that
runs out is refused, 403, with `used` pinned at `limit` across the five headers, ahead of the API
version check, the 401 a route that needs a credential answers a caller without one, and routing
alike, for a caller with no credential (measured against api.github.com 2026-09-17 and 2026-09-22)
and for a token (2026-09-23) alike; a bearer that does not resolve still gets its own 401, window
spent or not (2026-09-23). The refused request is not itself counted, which is why `used` holds at
`limit` rather than climbing past it. The body differs by caller: two members (`message`,
`documentation_url`) for a caller with no credential, three (`status` besides) for a token, each
with its own `documentation_url`. `BACKLOT_GITHUB_ENFORCE_RATE_LIMITS` turns the refusal off (see
[configuration](configuration.md#github)). `GET /rate_limit` reports the same windows, does not
count, and is never refused — the one route a client reads its way out of a spent window with;
`/rate_limit/` asked with no `Authorization` header is a plain-text `404 Not Found` that carries
none of the five and counts nowhere, spent window or not (2026-09-23). Two answers carry none of the
five and count nowhere, as real's do not: a credential that does not resolve, and a path no route
matches asked by a caller that sent one (measured 2026-09-10 and 2026-09-21).

An issue body and a pull body are the two distinct field sets real serves — a pull carries `_links`
and its `*_url` siblings and none of the issue-only fields, `pull_request` included. A repository
carries a URL template for each sub-resource Backlot actually serves, and none for the ones it
doesn't: following a link is supposed to reach something. The `{owner}` segment is validated against
the served org and 404s otherwise, as GitHub does.

A pull's changed-file list comes from its corpus `changed_paths` when declared and is chosen
deterministically otherwise; either way the hunks are derived from each file's own snapshot, so the
diff applies with real `git` and `additions`/`deletions`/`changed_files` agree with `/files`. A
comment carrying a `path` is served as a line-anchored review comment, kept apart from the
conversation as GitHub keeps them.

Code search answers one hit per `(repo, path)` — the head snapshot's — because real indexes the
default branch only; an older snapshot stays reachable at `contents/{path}?ref=` and
`git/blobs/{sha}`. Both searches page with an RFC5988 `Link`, as every listing on this surface does.

### Gmail — `/gmail/v1`

| Endpoint | Notes |
|---|---|
| `users/{u}/messages` | `q`: free text / `from:` `to:` `subject:` `after:` `before:` `newer_than:` `older_than:` `label:` `has:attachment` |
| `users/{u}/messages/{id}` | `format=full\|metadata\|minimal`, `metadataHeaders` |
| `users/{u}/messages/{id}/attachments/{id}` | |
| `users/{u}/threads` | `q`, as above |
| `users/{u}/threads/{id}` | `format`, `metadataHeaders`, as above |
| `users/{u}/labels[/{id}]` | |
| `users/{u}/profile` | |

Message and thread ids are Gmail-shaped — 16 lowercase hex under 2^63, sharing one id space as the
real API does — and map back to the corpus document; an id the real API could not parse is refused
the same way.

### Google Drive, Docs, Sheets, Slides — `/drive/v3` `/docs/v1` `/sheets/v4` `/slides/v1`

One `source_type` (`google_drive`) across four prefixes.

| Endpoint | Notes |
|---|---|
| `/drive/v3/files` | `q`, parsed as the reference's grammar (`and`, `or`, `not`, parentheses, `\'` inside a value): `name` and `mimeType` with `contains`/`=`/`!=`, `fullText contains`, `modifiedTime` and `createdTime` with `<`/`<=`/`=`/`!=`/`>`/`>=`, `trashed`, `sharedWithMe`, `… in parents` incl. `'root'`, `… in owners`. A term Backlot cannot evaluate is a 400 on `q`, never a silently unfiltered listing. `orderBy`: `name`/`name_natural`/`createdTime`/`modifiedTime`/`recency`/`folder`/`starred`/`quotaBytesUsed`/`sharedWithMeTime` (+` desc`). `fields` projection, validated |
| `/drive/v3/files/{id}` | `fields` |
| `/drive/v3/files/{id}/export` | |
| `/drive/v3/files/{id}/permissions` | |
| `/drive/v3/drives` | |
| `/drive/v3/about` | `fields` **required**, as in real Google Drive; `storageQuota` is measured from the caller's visible corpus |
| `/docs/v1/documents/{id}` | |
| `/sheets/v4/spreadsheets/{id}` | One entry per sheet, with its own `sheetId`, `index`, `title` and `gridProperties`. Structure only — cells need `includeGridData=true`, as in real Sheets. `ranges` filters the `sheets` array itself, and gives a sheet one `data` block per range that touches it |
| `/sheets/v4/spreadsheets/{id}/values/{range}` | A1 ranges incl. `Summary!A1:B2`, `A:A`, `1:3`, `A2:B`, a bare sheet name quoted or not, an empty one before the bang (`!A1` is the first sheet), and R1C1 — absolute (`R1C1:R2C2`) and bracketed offsets from A1 (`R[1]C[1]`), echoed as the A1 equivalent, with real's reversed-range rule. Any sheet in the workbook, matched case-insensitively; an unqualified range answers from the sheet at index 0. `majorDimension`, `valueRenderOption` |
| `/sheets/v4/spreadsheets/{id}/values:batchGet` | As above; one unparseable range fails the whole call |
| `/sheets/v4/spreadsheets/{id}:getByDataFilter` | The same read addressed by `DataFilter` (an `a1Range`, a `gridRange` or a `developerMetadataLookup`) instead of `ranges`. A read, over POST because the filters do not fit in a query string; no filter means every sheet, and so do lookups alone, since a corpus states no developer metadata for one to match. Two filters covering the same cells get a single `data` block |
| `/sheets/v4/spreadsheets/{id}/values:batchGetByDataFilter` | Likewise for values. Each entry carries the filters that selected it, echoed as the proto real read, filters whose answers name the same range sharing one entry, and the entries come back ordered by where each range starts rather than as sent. A `gridRange` with no cells answers `#REF!`, and a lookup no entry |
| `/slides/v1/presentations/{id}` | |

The three editor APIs serve native-doc content for editor-aware clients, read structurally instead
of via Google Drive export.

Folders are files here: they match `mimeType='…folder'`, project, sort and resolve permissions like
stored rows. Trashed files are excluded unless `trashed = true` asks for them.

A spreadsheet has two shapes, and the corpus record picks which. A record that states `sheets` is a
real workbook: named sheets over a 2D grid whose cells keep the type the corpus gave them, so
`valueRenderOption=UNFORMATTED_VALUE` answers a JSON number where `FORMATTED_VALUE` answers a
string. A record that states only `content` keeps the older reading — one sheet, each stored **line**
held in a single cell verbatim, with Backlot picking no column delimiter, so splitting (CSV, pipes,
…) stays the corpus owner's decision. Either way `files.export` and the Sheets API describe the same
cells. Reading a file of the wrong type through any of the three editor APIs is refused, as real
Google does, not reinterpreted.

#### OAuth and batch

Two more Google-shaped routes, both at the **server root** rather than under the prefixes above,
because that is where Google puts them.

| Endpoint | Notes |
|---|---|
| `POST /oauth2/token` | Turns a Google-style client credential into a bearer token the rest of Backlot already understands. Two grants: `refresh_token`, where the refresh token *is* the user's token from `/_meta/users`, and a signed service-account JWT assertion, whose `sub` claim selects the impersonated user under domain-wide delegation. A bare service account with no `sub` resolves to the admin/service identity. Expiry is cosmetic — a re-refresh returns the same token, so a long crawl never breaks |
| `POST /batch`, `POST /batch/{api}/{version}` | Google's `multipart/mixed` batch envelope: each part is an `application/http` sub-request, answered in order with its `Content-ID` preserved. The outer credential applies to any sub-request that does not carry its own, as real Google does. As in real's Drive batch, a Drive download (`files.export` with no `alt`, an empty one or `alt=media`, or `files.get` with `alt=media`) is a 302 to the same path under `/download`, which this server does not serve, after `$.xgafv` and after a `Bearer` credential the part carries (a part with none, on it or on the batch, is redirected too), and ahead of `callback` and the typed, lookup, `fields` and `mimeType` refusals; as in real's Sheets batch, a Sheets read is 501 `UNIMPLEMENTED` |

`/batch` is Google-shaped but not Google-scoped — sub-requests are dispatched against the whole
app, so a batch may target any endpoint this server serves, not only Google Drive's.

**`$.xgafv` is honoured on every Google route**, the way real declares it: a system parameter at
the top of the discovery document, which every method takes, rather than one a few methods list.
What it selects is the legacy `errors[]` array, and the three families answer it three ways. Docs,
Sheets and Slides opt in — `1` adds the array, `2` and an absent value leave it off. Gmail opts
out — the array is there unless `2` turns it off. Drive carries it whatever the value says. A
success body is the same under all of them. A value other than `1` or `2` is refused before
anything else is read, ahead of a bad token or an unparseable range, with real's sentence
`Invalid query parameters. Invalid value '…' for system query parameter : $.xgafv`. Inside the
array the entry follows the error: a typed value the proto layer refuses (an enum, a bool, an
int32), an unknown member in a JSON body and a JSON body that is not an object are `reason: invalid`
and carry no `domain`; under `domain: global`, a body the JSON parser refuses in its own words is
`parseError`, an Office file read as a native document `failedPrecondition`, a data-filter read's
500 `backendError` and everything else `badRequest`; and a missing credential — any anonymous POST,
the two Sheets data-filter reads included, and a GET on Gmail, Docs and Slides — is the short
`Login Required.` at `location: Authorization`, which Gmail shows by default where the editor
families show it only at `1`. Measured against the live Sheets, Docs and Drive APIs on 2026-09-12,
against Slides and Gmail on 2026-09-14 through the errors a request with no Authorization header
reaches, and against the Sheets data-filter POSTs on 2026-09-22, their bodies' refusals and 500 on
2026-10-04.

**Every Google error body is rendered the way real renders one** — two spaces deep with a trailing
newline whatever `prettyPrint` says, `application/json; charset=UTF-8`, and the 209 characters
real escapes written as `\uXXXX` — `<` and `>` among them, the C0 and C1 controls, the line and
paragraph separators, and the format characters as Unicode 4.0 drew that category, while letters,
emoji, NBSP, `&` and `'` stay as they are. `callback` turns one into JSONP: HTTP **200** with
`text/javascript; charset=UTF-8` and the body inside `// API callback\ncb({…}\n);`, which is what
lets a page loading the answer through a `<script>` element reach its error branch rather than
`onerror`. On an ordinary read, a name that cannot be a JavaScript one is refused with real's own
sentence — `only alphabet, number, '_', '$', '.', '[' and ']' are allowed` — ahead of a bad token, a
missing credential, an unparseable range and a mistyped `fields` mask, though `$.xgafv` is refused
ahead of it and an `alt` naming a format other than `json` suppresses the wrap altogether — the
format is matched without regard to case and an empty `alt=` names none, so `alt=JSON`, `alt=Json`
and `alt=` each ask for the JSON the default serves rather than for a format of their own. An empty
`callback=` is no callback, and a POST ignores the parameter outright, as real does, since JSONP is
what a `<script>` element fetches and a `<script>` element issues a GET. A Drive download — an
export with no `alt`, an empty one or `alt=media`, or a `files.get` with `alt=media` — is not JSONP
at all: a `callback` there is never wrapped, is ignored when the download succeeds, and turns every
other answer into 503 `Backend Error` under `text/javascript; charset=UTF-8` with a body of real's
own (the `errors[]` entry inline and no trailing newline), while a name that cannot be called is
that 503 even where the download would have succeeded. A missing credential on one names the
missing API key rather than the unregistered caller, and only after the download's `mimeType` and
typed-parameter checks, so the order a byte-stream read answers in is `$.xgafv`, a `Bearer` that
does not resolve, the `callback`, the parameters, then that 403. Inside a batch a download is a 302
instead (the `/batch` row above). A SUCCESS body is wrapped and
indented on the `/sheets/v4` routes only; the other four families honour `callback` on their errors
and not yet on their 200s. Measured against the live Sheets, Docs, Drive, Gmail and Slides APIs on
2026-09-15, 2026-09-16 and 2026-09-17: the wrap, the indent and the charset first, the suppression
across the four non-Sheets families next, and the escape set and the case-insensitive `alt` last;
the download refusals on 2026-10-04, 2026-10-05 and 2026-10-07.

**A repeated query parameter is read from the end real reads it from**, which is the first for some
parameters and the last for others. The first repeat decides `fields`, `q`, `pageSize`, `pageToken`
and `orderBy` on Drive's `files.list`, `pageToken` on `permissions.list` and `drives.list`, `fields`
on `files.get` and `about`, and `mimeType` on `files.export`, and on Sheets `fields` and
`prettyPrint`, as it decides `callback` and `alt`; the last decides `$.xgafv`, `majorDimension`,
`valueRenderOption` and `includeGridData`. An empty first repeat is read as the empty value, not
skipped. Gmail's `q`, `pageToken` and `maxResults` are read here from the last, and which end real
reads is unmeasured. On a Sheets success, `prettyPrint` is compact at `false` and `0` and at none of
the eighteen other spellings measured, `FALSE`, `no` and `f` among them. Measured against the live
Drive, Sheets and Gmail APIs, each pair sent both ways round: `callback`, `alt` and the Sheets
`$.xgafv` between 2026-09-15 and 2026-09-17, `includeGridData` and the Gmail `$.xgafv` on
2026-09-22, `pageToken` on the other two Drive listings on 2026-10-05 and 2026-10-07, and the rest
on 2026-09-23.

**A typed query parameter is parsed in every repeat, and every value it cannot read is refused in
one 400**: the message joins theirs with newlines and `details` carries a `google.rpc.BadRequest`
field violation for each, a parameter's repeats together and in query order, on Sheets'
`majorDimension`, `valueRenderOption`, `dateTimeRenderOption`, `includeGridData` and
`excludeTablesInBandedRanges`, Drive's `pageSize` and the booleans each served Drive method declares
(`supportsAllDrives`, `includeItemsFromAllDrives`, `acknowledgeAbuse`, `useDomainAdminAccess` and
the two deprecated team-drive ones) alike, and a Sheets data-filter body's values carry the same
`details`. A typed refusal comes after the credential check and before the file or spreadsheet is
looked up. On Drive's `files.list`, one `pageSize` outside 1-1000 is refused with the range sentence
(1-100 on `permissions.list` and `drives.list`), while a repeated one is read from the first and
never range-checked; a `pageToken` it did not issue is 400 `Invalid Value`; and the refusals come in
the order `pageSize`, `orderBy`, `q`, an `orderBy` beside a `fullText` term in `q` (403
`forbidden`), `pageToken`, `fields`, an `orderBy` naming `starred` after another key being real's
500 `Internal Error` between `pageToken` and `fields`. `permissions.list` and `drives.list` issue no
`nextPageToken` and refuse every non-empty `pageToken` with that 400, and `permissions.list` an
empty one with 403 `pageTokenExpired`, after the typed and range refusals and ahead of the
`useDomainAdminAccess=true` refusal and `permissions.list`'s file lookup. A blank `fields` on
`files.list` or `files.get` answers `{}`. `files.export` refuses a format the file's type does not
export to, the empty `mimeType=` among them, with `The requested conversion is not supported.`,
matching the format without regard to case, refuses an absent `mimeType` ahead of looking the file
up, and serves an export under the `mimeType` exactly as sent, with no `charset`. Measured against
the live Drive and Sheets APIs on 2026-09-23, the `fullText` 403 on 2026-10-05 and 2026-10-07, the
`starred` 500 on 2026-10-04 and 2026-10-07, the `pageToken` of `permissions.list` and `drives.list`
on 2026-10-04, 2026-10-05 and 2026-10-07, and the export's `Content-Type` on 2026-09-30.

**Four Drive flags spelled `true`, in any case, run a check of their own**, where `1`, `t` and `yes`
parse as true and run none. `includeItemsFromAllDrives` or `includeTeamDriveItems` on `files.list`
without `supportsAllDrives` or `supportsTeamDrives` spelled the same way is 403
`supportsTeamDrivesRequired`, between the `orderBy` and `q` refusals. `acknowledgeAbuse` on a
`files.get` that downloads nothing is 403 `invalidAbuseAcknowledgment`, before the file is looked
up, and inside a batch only on its one part that is not a download. `useDomainAdminAccess`, since
every caller here is a Workspace member and none is its domain's administrator, is 404
`File not found` on `permissions.list` and 403 `noListTeamDrivesAdministratorPrivilege` on
`drives.list`. Each flag is read from its first repeat. Measured against the live Drive API as such
a member on 2026-10-06, on its own and in a batch.

**A Sheets read enum is taken by its name in any ASCII case, with `-` for `_`, or by the number the
name has**, in the query string and in a data-filter body alike, and `DIMENSION_UNSPECIFIED` reads
as `ROWS`. A data-filter body is written into the request message the way real's proto writer
writes it: every value it cannot convert and every name the request message lacks is refused in one
400, each named by its proto path where it has one. Measured against the live Sheets API on
2026-10-04, and a non-ASCII letter's case on 2026-10-05.

### HubSpot — `/hubspot/crm/v3` `/hubspot/crm/v4`

| Endpoint | Notes |
|---|---|
| `v3/objects/{objectType}` | `limit` max 100, `after`, `properties`, `archived` |
| `v3/objects/{objectType}/{id}` | |
| `POST v3/objects/{objectType}/search` | `filterGroups` OR-ed, `filters` AND-ed, 13 operators over any property |
| `POST v3/objects/{objectType}/batch/read` | |
| `v4/objects/{type}/{id}/associations/{toType}` | |

The CRM API is polymorphic over `{objectType}`, so these five work across every object type rather
than there being a set per type.

### Jira — `/atlassian/rest/api/3` (and `/2`)

| Endpoint | Notes |
|---|---|
| `search/jql` | `GET` or `POST`. JQL `project =`, `text`\|`summary`\|`description` `~` |
| `issue/{key}` | |
| `issue/{key}/comment` | `startAt`, `maxResults` (max 100), `orderBy` `created`/`+created`/`-created` |
| `field` | |
| `issueLinkType` | |
| `project/search` | |
| `project/{key}/role[/{id}]` | |
| `serverInfo` | |

`search/jql`, `issue/{key}`, `issue/{key}/comment`, `field` and `serverInfo` are served under
`rest/api/2` as well as `/3`.

Issue and comment reads accept the key or the exact numeric `id` reported by search and issue
responses. Numeric ids are assigned uniquely at import, including when keys hash alike, and remain
stable across appended shards. Databases built before `jira_issues.numeric_id` was added must be
re-imported; the startup schema check names the missing column.

A JSON body is `application/json;charset=UTF-8` — no space after the semicolon, `UTF-8`
upper-case — as real's is on every route and status measured, except where real answers a
different type altogether: the RFC 7807 refusals (a type-conversion 400, the 405, the 415) are
`application/problem+json;charset=UTF-8`, and the gateway's 403 for a bearer it cannot read as a
Connect token is the bare `application/json`.

A trailing slash is not part of a path on either product, and a run of slashes inside one is a
single slash — both spellings answer what the canonical one answers, though a refusal echoes the
path with its trailing slash kept. A `HEAD` is the `GET` with the body left off and declares no
length but on Jira's 401 to an unauthenticated caller, which is where Jira parts from Confluence. An
`OPTIONS` is 200 for a caller whose credential resolves, with an empty `text/html` body, an empty
`Accept-Patch`, an `Allow` naming the methods the vendor serves at that route — the `PUT` and
`DELETE` on an issue among them, which Backlot does not serve — and a quota of its own; anyone else
gets Jira's 401. A `PATCH` never reaches either product: the gateway answers 400 on Jira and 405 on
Confluence. Nor does a method the CDN refuses itself: `TRACE` and `CONNECT` are its 405, and any
other method it does not pass on is its 403 or 400 by how the method is spelled. A path no route
serves is RFC 7807 at 404 with `No endpoint <METHOD> <path>.` where Jira publishes nothing at it; at
an operation it publishes and no route here serves, a `Content-Type` the operation does not take is
its 415 for any caller the gateway lets through, and after that a caller with no credential gets
Jira's 401 `Client must be authenticated to access this resource.` where the operation will not run
anonymously (`backlot/data/jira_unserved.json`), and otherwise that 404, the gap the baseline
acknowledges. That is under `/atlassian/rest` only: outside the two API mounts the site is its web
app, `/browse` at 200, the root a redirect to log in or to `/jira/for-you`, and Jira's own not-found
page for the rest. Every answer past the CDN carries `atl-request-id`, `atl-traceid`,
`x-content-type-options` and `x-xss-protection`, and Jira's own answers add `x-arequestid`,
`cache-control` and `timing-allow-origin`, which the gateway's refusals (the Connect-token 403, a
`PATCH`) do not carry; a caller whose credential resolves also gets its own `x-aaccountid`, and the
burst quota's four (`ratelimit`, `ratelimit-policy`, `x-ratelimit-limit`, `x-ratelimit-remaining`)
where a route answers or an `OPTIONS` asks at one, counted per method and route. An anonymous
request carries none of those five, and the no-endpoint 404, a 405 and an unserved operation's 415
carry the account id alone.

### Linear — `/linear/graphql`

**GraphQL only**, one `POST`. Root `Query` fields:

| Query field | Notes |
|---|---|
| `issues` | |
| `issue(id:)` | UUID *or* `ENG-123` |
| `team(id:)` | UUID, key, or name |
| `teams` | |
| `comments` | |
| `users` | |
| `viewer` | |

Plus the `Team.issues` and `Issue.{comments,labels,children,relations,inverseRelations,attachments,releases}`
connections, and the by-id roots (`user`, `workflowState`, `project`, `issueLabel`, `cycle`,
`release`, `attachment`, `issueRelation`) the official SDK's lazy relation accessors call.

Relay pagination (`first`/`after`, `last`/`before` → `{nodes, pageInfo}`), server-side `filter`
compiled into SQL, and full introspection.

### Notion — `/notion/v1`

Every route requires a `Notion-Version` header and answers `missing_version` without one — or
with a version Notion does not publish — as the real API does; the value picks the database model,
which is what the notes below name.

A URL Notion does not publish is `invalid_request_url` at 400, which the real API answers before it
reads the credential or the version. The method is part of that URL: a `GET` on a route that
answers `POST` is the same 400 rather than a 405, with `TRACE` the exception real refuses at the
front door with Cloudflare's own 405 page. An operation Notion publishes and Backlot does not serve
(`PATCH pages/{id}`, `POST comments` and the other `missing_operation` rows in
`backlot/fidelity/baseline/notion.json`) answers the credential's 401 as a served route does, and
a request that clears the credential and the version gets the 400, as there is no operation to run.
The three OAuth client endpoints among them, `POST oauth/token`, `oauth/introspect` and
`oauth/revoke`, answer `{"error":"invalid_client"}` at 401 instead, whatever the credential, as
Backlot registers no OAuth client. One trailing slash is not part of a URL (`users/me/` is served
as `users/me`, where a second slash is a segment and gets the 400), and a `HEAD` is the `GET` with
the body left off. A path in another case is the 400 here, where real routes it as the lower-case
path, and `PROPFIND` or `QUERY` is the framework's 405 here, where real answers the URL's 400. A
credential that is not `Bearer <token>` (no header, no scheme, `Basic`, GitHub's legacy
`token <t>`, or a bearer with a second word after its token) is refused by naming the format, where
a bearer whose token does not resolve is `API token is invalid.`. Each refusal named here but the
framework's 405 carries a `request_id` in the body and `x-notion-request-id` on the response, one
value; real's differs per response and this one is derived from the request, so a corpus served
twice answers the same id.

| Endpoint | Notes |
|---|---|
| `POST search` | |
| `pages/{id}` | |
| `blocks/{id}` | |
| `blocks/{id}/children` | |
| `databases/{id}` | Version-aware: `data_sources` from `2025-09-03`, inline `properties` before it |
| `POST databases/{id}/query` | Versions before `2025-09-03` only |
| `data_sources/{id}` | `2025-09-03` and later only |
| `POST data_sources/{id}/query` | `2025-09-03` and later only |
| `users[/{id}]` | |
| `users/me` | |
| `comments` | |

### Amazon S3 — `/s3`

Addressed as S3 operations rather than paths, which is how the AWS SDKs and CLI reach them. Point a
client at `/s3` with **path addressing** — the bucket belongs in the path, not the host, so a
virtual-hosted client looks for `acme-artifacts.localhost:8000` and finds nothing.

| Operation | Notes |
|---|---|
| `ListBuckets` | `max-buckets`, `prefix`, `continuation-token` and `bucket-region`, paged and refused as real pages and refuses them, with each bucket's region beside its ARN once one is sent. An unsigned request is real's 307 to the product page |
| `HeadBucket` | The listing's parameters are refused as the listing refuses them, apart from the `max-keys` range, as on real |
| `GetBucketLocation` | |
| `GetBucketVersioning`, `GetBucketAcl` and the rest of a bucket's configurations | Each as real answers a bucket nobody configured, byte for byte: the default where real has one (`?acl` the owner's `FULL_CONTROL`, `?encryption` SSE-S3, `?versioning` and `?accelerate` empty) and real's 404 where it has none (`NoSuchCORSConfiguration`, `NoSuchBucketPolicy`, `NoSuchTagSet` and the rest). The four configuration lists are empty and read `id` and `continuation-token` as real does |
| `ListObjectVersions` | Every key as its one version, `null` and the latest, since no bucket here is versioned. `prefix`, `delimiter`, `key-marker`, `version-id-marker`, `max-keys`, `encoding-type`, paged and refused as real pages and refuses them |
| `ListMultipartUploads` | Always the empty page, since data enters through `backlot import` and no upload is ever in progress. `prefix`, `delimiter` and `key-marker` are echoed, `max-uploads` and `encoding-type` validated and echoed, as real does |
| `ListObjects` | The bare bucket GET, and what any `list-type` other than `2` selects. `prefix`, `delimiter`, `marker`, `max-keys`, `encoding-type`; `Marker` echoed, `NextMarker` under a delimiter, an `Owner` on every object |
| `ListObjectsV2` | Selected by `list-type=2`. `prefix`, `delimiter`, `start-after`, `continuation-token`, `max-keys`, `encoding-type`, `fetch-owner`; `KeyCount` and the continuation tokens, an `Owner` on every object only under `fetch-owner=true` |
| `GetObject` | `Range`; `partNumber`, where part `1` is the whole object as the 206 of its range, a higher part real's 416 and a number outside 1 to 10000 or one beside a `Range` real's 400; `versionId=null`, the one version each key has, where any other `versionId` is real's 400; and checksum mode, where an answer holding the whole object carries its CRC-64/NVME and another mode is real's 400. A key in a bucket that does not exist, or that the caller cannot see, is `NoSuchBucket`, as on real |
| `HeadObject` | |
| `GetObjectAcl`, `GetObjectTagging`, `GetObjectAttributes` and the rest of an object's sub-resources | Each as real answers an object with no tags or annotations, written with no checksum header, in a bucket without Object Lock, byte for byte: `?acl` the owner's `FULL_CONTROL`, `?tagging` an empty `TagSet`, `?attributes` the ETag, checksum, storage class and size asked for, `?annotation` no annotations and `NoSuchAnnotation` for one named, `?legal-hold` and `?retention` real's 400 for a bucket without Object Lock, and `?torrent` real's 405 |
| `ListParts` | Always `NoSuchUpload`, since no upload is ever in progress; `max-parts` and `part-number-marker` validated first, as real does |

The other sub-resources are answered as real answers them. `?session` is the listing: CreateSession
is for directory buckets only and real S3 answers it with the listing on a general purpose bucket. A
sub-resource whose operations are all on another method, `?delete` and a key's `?restore` and
`?select` among them, is the 405 a GET or a HEAD naming it gets, and a key's `?uploads` on a GET is
real's 400. A `?partNumber` at a bucket's path, which names a part of an object, is real's 400 on a
GET, a HEAD, a `PUT`, a `POST`, a `DELETE` and a `PATCH`, and beside `?uploadId` it is UploadPart's
405. At a bucket's path `?torrent` is real's 405 and `?uploadId` real's 400 once the bucket is
found. At a key's path `?accelerate`, `?cors`, `?inventory`, `?lifecycle`, `?location`,
`?notification`, `?policy`, `?replication`, `?requestPayment`, `?versioning` and `?website` are the
bucket's answer whatever the key, and `?logging` and `?versions` real's 400s; a bucket's other
sub-resources are ignored there, as on real. A `versionId` at a bucket's path is real's 400 on a GET
and on the writes that refuse it there, and at a key's beside a selector whose operation takes none,
a `?website` with a value real's 400 at either path, and an `annotationName` at a key without the
`annotation` it belongs to real's 400, which most of real's front ends answer a GET with and a few
answer as if it were absent. Two sub-resources at once are `InvalidArgument`, as on real S3, and an
unknown query key is ignored, as on real S3.

A method no operation above serves answers what real answers: the 405 that names the method and
whether the resource is a `BUCKET`, an `OBJECT` or the `SERVICE`, the 400 an `OPTIONS` without an
`Origin` gets from real's CORS front end and the 403 it gets with one, and the 412 a bucket `POST`
gets. A sub-resource selector decides for itself, as on real: `PATCH ?acl` is the 405 naming `ACL`,
and two selectors are the conflict a GET gets. The methods real answers by writing — a `PUT` or
`DELETE` on a key, a `DELETE` on a bucket, a bare bucket `PUT` (CreateBucket), and a selector's own
write method such as `POST ?delete`, `PUT ?acl` or a key's `POST ?uploads` — are `NotImplemented`
(501), since the corpus is served as it was imported. Every one of them but CreateBucket first
resolves the bucket it names, and one that does not exist, or that the caller cannot see, is
`NoSuchBucket` instead, as on real. Four are then checked as real checks them before it writes: a
bucket's `POST ?restore`, which names no object, is real's 400; `POST ?delete`, at a bucket's path
or a key's, is refused for its checksum headers, its `Content-MD5`, a body real's schema does not
take, an empty key and a digest the body does not match; a key's `PUT ?encryption` for a Signature
Version 2 signature, its `Content-MD5`, its body, the key and the KMS key's ARN; and a key's `PUT`
(PutObject) for its `Content-MD5`, its checksum headers, an aws-chunked payload hash with no decoded
length (real's 411) and a digest the body does not match. A `PUT` or a `POST` whose signed
`x-amz-content-sha256` is a digest the body does not have is refused with real's
`XAmzContentSHA256Mismatch`, which real named on PutObject, `PUT ?tagging`, `PUT ?versioning`,
`POST ?delete` and `PUT ?encryption`, and one naming a trailer on any operation but PutObject with
the 400 real answered a read, a `DELETE`, `POST ?delete` and `PUT ?tagging` with. The `Allow` on a
405 names what Backlot serves rather than real's own methods. A method S3 defines nothing for at
all, `TRACE` among them, is the 400 real answers it with rather than a 405.

A signed call is SigV4, SigV4a or Signature Version 2, in the header or the query, each of which
real verifies; see [auth.md](auth.md). The region a SigV4 credential scope names has to be
`us-east-1`, the one this server presents, and another is real's `AuthorizationHeaderMalformed` in
the header and `AuthorizationQueryParametersError` in the query, naming both regions; a SigV4a
region set has to name it too, and one that does not is real's `RegionSetMismatch`. A SigV4 or
SigV4a header has to carry an `x-amz-content-sha256` real takes, and a header real requires to be
signed, `Host`, `Content-MD5` or an `x-amz-*` one but `x-amz-content-sha256`, that is sent unsigned
is real's `AccessDenied` naming it (`HeadersNotSigned`). An unsigned one is an anonymous caller's,
as on real, and an anonymous caller can see no bucket, so it is `NoSuchBucket` wherever a signed
caller that cannot see the bucket would be; real says `AccessDenied` for a bucket that exists, which
would tell an unsigned caller which names the corpus holds. The method refusals above answer an
unsigned request as a signed one, as on real, and so do the 405 a GET or a HEAD gets for a
sub-resource, the conflict of two and the parameter refusals before the bucket. A signature that is
sent and does not verify is refused ahead of each 405, with real's members naming the string this
server signed and, for SigV4 and SigV4a, the canonical request it signed it over, and after the
conflict and the parameter refusals, as on real. Each other credential refusal is real's own code
and message too.

### Slack — `/slack/api`

Each method answers on `GET` and `POST` alike, as the real Web API does.

| Method | Notes |
|---|---|
| `conversations.list` | `types` defaults to `public_channel` as the real API does — pass `public_channel,private_channel` to crawl both. This corpus has no DMs, so `im`/`mpim` select nothing, and an unknown value is `invalid_types` |
| `conversations.info` | |
| `conversations.history` | `oldest`, `latest`, `inclusive` |
| `conversations.replies` | |
| `conversations.members` | Per-channel, paginated |
| `users.list` | |
| `users.info` | |
| `search.messages` | |
| `search.all` | |
| `search.files` | |
| `auth.test` | |
| `api.test` | Auth-free connectivity check |

A person the [roster](../backlot/schemas/README.md) marks `deactivated: true` is Slack's offboarded
member: `users.list` and `users.info` answer them `deleted: true`, `conversations.members` and
`num_members` drop them from every channel while their messages stay in channel history, and their
own token is answered `account_inactive`. Slack alone draws it — the same token still reads every
other source, because the state is Slack's rather than an org-wide suspension.

A channel the caller cannot see is refused by id as well as hidden from the listing:
`conversations.info`, `.members`, `.history` and `.replies` all answer `channel_not_found`, the same
answer an id that names nothing gets, so a private room's name, purpose and membership are not
readable from its id alone. A required argument that was never sent is `invalid_arguments` rather
than a `not_found` for something the caller never named.

## Backlot's own endpoints

Not part of any vendor's API — Backlot's own.

| Endpoint | Notes |
|---|---|
| `/health` | Liveness, plus two corpus counts: `documents` is the root rows served, `source_documents` is what the corpus offered — smaller, because parsing turns one Slack transcript into many messages |
| `/_meta/users` | Every generated user with their token and groups, in `data/tokens.yaml`'s shape, plus an `s3_access_key_id` / `s3_secret_access_key` pair each, since S3 authenticates with SigV4 rather than a bearer token. Pick a token, send it to any service, and see that user's ACL-filtered view. This is also what `backlot mcp --user <email>` resolves a person through, so it is always served |
| `/_meta/credentials` | The shared Google-style OAuth client and the org service account, for connectors that configure with an OAuth client instead of a raw token. No per-user data — a user's refresh token is their bearer token from `/_meta/users` |
| `/_meta/openapi/{source}` | One source's slice of `/openapi.json`, with each operation named for its route and the GET/POST and Jira v2/v3 fidelity aliases collapsed to one operation each, ready to hand to `FastMCP.from_openapi()`. HEAD is dropped: a response with no body cannot answer the question that called it. S3 is here too — SigV4 signs each request, so the bridge signs rather than holding a fixed header |
| `/openapi.json` | FastAPI's own typed spec for the whole server |

`/_meta/users` and `/_meta/credentials` hand out working credentials in the clear — which is what
they are for on a server whose whole corpus is a fixture. See [auth.md](auth.md).
