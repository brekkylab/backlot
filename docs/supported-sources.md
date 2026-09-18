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
| `github` | GitHub | `/github` | 33 | [`github.schema.json`](../backlot/schemas/github.schema.json) | A GitHub issue, pull request, file, or the repository itself. |
| `gmail` | Gmail | `/gmail/v1` | 8 | [`gmail.schema.json`](../backlot/schemas/gmail.schema.json) | A Gmail message. |
| `google_drive` | Google Drive, Docs, Sheets, Slides | `/drive/v3` `/docs/v1` `/sheets/v4` `/slides/v1` | 13 | [`google_drive.schema.json`](../backlot/schemas/google_drive.schema.json) | A Google Drive file. |
| `hubspot` | HubSpot | `/hubspot` | 5 | [`hubspot.schema.json`](../backlot/schemas/hubspot.schema.json) | A HubSpot CRM record (contact, company, deal, ticket, note, …). |
| `jira` | Jira | `/atlassian/rest/api` | 14 | [`jira.schema.json`](../backlot/schemas/jira.schema.json) | A Jira issue. |
| `linear` | Linear | `/linear/graphql` | GraphQL (one `POST`) | [`linear.schema.json`](../backlot/schemas/linear.schema.json) | A Linear issue. |
| `notion` | Notion | `/notion/v1` | 12 | [`notion.schema.json`](../backlot/schemas/notion.schema.json) | A Notion page or database. |
| `s3` | Amazon S3 | `/s3` | 4 | [`s3.schema.json`](../backlot/schemas/s3.schema.json) | An S3 object. |
| `slack` | Slack | `/slack/api` | 21 | [`slack.schema.json`](../backlot/schemas/slack.schema.json) | A Slack message. |
<!-- generated:sources end -->

## Per-service detail

Ordered as the table above, by `source_type`.

### Confluence — `/atlassian/wiki/rest/api`

| Endpoint | Notes |
|---|---|
| `content` | |
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
resource in an hourly window, `reset` the second that window closes, `resource` the one the request
counted against. Nothing is refused when a window runs out: `remaining` stops at 0 and `used` keeps
counting, so a client that paces by the headers sees its real pace and a test suite is never failed
for its own volume. `GET /rate_limit` reports the same windows and does not count (measured
2026-09-10).

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
| `users/{u}/messages/{id}` | `format=full\|metadata\|minimal` |
| `users/{u}/messages/{id}/attachments/{id}` | |
| `users/{u}/threads` | `q`, as above |
| `users/{u}/threads/{id}` | |
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
| `/sheets/v4/spreadsheets/{id}:getByDataFilter` | The same read addressed by `DataFilter` (an `a1Range` or a `gridRange`) instead of `ranges`. A read, over POST because the filters do not fit in a query string; no filter means every sheet |
| `/sheets/v4/spreadsheets/{id}/values:batchGetByDataFilter` | Likewise for values. Each entry carries the filter that selected it, and the entries come back ordered by where each range starts rather than as sent |
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
| `POST /batch`, `POST /batch/{api}/{version}` | Google's `multipart/mixed` batch envelope: each part is an `application/http` sub-request, answered in order with its `Content-ID` preserved. The outer credential applies to any sub-request that does not carry its own, as real Google does |

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
int32) is `reason: invalid` and carries no `domain`; an Office file read as a native document is
`failedPrecondition` under `domain: global`; everything else is `badRequest` under the same domain;
and a missing credential on any of the three OAuth-only APIs — Gmail, Docs and Slides — is the
short `Login Required.` at `location: Authorization`, which Gmail shows by default where the editor
families show it only at `1`. Measured against the live Sheets, Docs and Drive APIs on 2026-09-12,
and against Slides and Gmail on 2026-09-14 through the errors a request with no Authorization
header reaches.

**Every Google error body is rendered the way real renders one** — two spaces deep with a trailing
newline whatever `prettyPrint` says, `application/json; charset=UTF-8`, and the 209 characters
real escapes written as `\uXXXX` — `<` and `>` among them, the C0 and C1 controls, the line and
paragraph separators, and the format characters as Unicode 4.0 drew that category, while letters,
emoji, NBSP, `&` and `'` stay as they are. `callback` turns one into JSONP: HTTP **200** with
`text/javascript; charset=UTF-8` and the body inside `// API callback\ncb({…}\n);`, which is what
lets a page loading the answer through a `<script>` element reach its error branch rather than
`onerror`. A name that cannot be a JavaScript one is refused with real's own sentence — `only
alphabet, number, '_', '$', '.', '[' and ']' are allowed` — ahead of a bad token, a missing
credential, an unparseable range and a mistyped `fields` mask, though `$.xgafv` is refused ahead of
it and an `alt` naming a format other than `json` suppresses the wrap altogether — the format is
matched without regard to case and an empty `alt=` names none, so `alt=JSON`, `alt=Json` and
`alt=` each ask for the JSON the default serves rather than for a format of their own. An empty
`callback=` is no
callback; a repeated one is answered through the first name where `$.xgafv` is answered through the
last; and a POST ignores the parameter outright, as real does, since JSONP is what a `<script>`
element fetches and a `<script>` element issues a GET. A SUCCESS body is wrapped and indented on
the `/sheets/v4` routes only; the other four families honour `callback` on their errors and not yet
on their 200s. Measured against the live Sheets, Docs, Drive, Gmail and Slides APIs on 2026-09-15,
2026-09-16 and 2026-09-17: the wrap, the indent and the charset first, the suppression across the
four non-Sheets families next, and the escape set and the case-insensitive `alt` last.

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
| `ListBuckets` | |
| `HeadBucket` | |
| `GetBucketLocation` | |
| `ListMultipartUploads` | Always the empty page, since data enters through `backlot import` and no upload is ever in progress. `prefix`, `delimiter` and `key-marker` are echoed, `max-uploads` and `encoding-type` validated and echoed, as real does |
| `ListObjects` | The bare bucket GET, and what any `list-type` other than `2` selects. `prefix`, `delimiter`, `marker`, `max-keys`, `encoding-type`; `Marker` echoed, `NextMarker` under a delimiter, an `Owner` on every object |
| `ListObjectsV2` | Selected by `list-type=2`. `prefix`, `delimiter`, `start-after`, `continuation-token`, `max-keys`, `encoding-type`; `KeyCount` and the continuation tokens, no `Owner` |
| `GetObject` | `Range` |
| `HeadObject` | |

Any other sub-resource — `?versioning`, `?acl`, `?tagging`, `?uploadId` and the rest of what botocore
declares at a bucket's or an object's path — is refused with `NotImplemented` (501), so a client gets
an error to handle rather than the listing or the object's bytes parsed as something else. The one
exception is `?session`: CreateSession is for directory buckets only and real S3 answers it with
the listing on a general purpose bucket, so Backlot does too. Two sub-resources at once are
`InvalidArgument`, as on real S3, and an unknown query key is ignored, as on real S3.

Every call is SigV4-signed; see [auth.md](auth.md).

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
| `chat.postMessage` | `thread_ts` posts a reply in that thread |
| `chat.postEphemeral` | Stores nothing, as an ephemeral message is in no channel's history |
| `chat.update` | The author's own message |
| `chat.delete` | The author's own message, a corpus one included |
| `chat.getPermalink` | |
| `reactions.add` | |
| `reactions.remove` | The caller's own |
| `reactions.get` | |
| `reactions.list` | The caller's own, from writes rather than the corpus |

The nine below the reads take writes. They land in a per-server overlay and the corpus file is
never written; [Writes, and where they go](overlay.md) covers what a read then sees, how to read
the overlay back and how to reset it. `chat.postMessage`, `chat.postEphemeral`, `chat.update`,
`chat.delete`, `reactions.add` and `reactions.remove` accept a JSON body as well as a form, which
is what their `consumes` declares and where `slack_sdk` puts three of them.

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
