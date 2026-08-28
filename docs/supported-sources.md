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
| `confluence` | Confluence | `/atlassian/wiki/rest/api` | 10 | [`confluence.schema.json`](../backlot/schemas/confluence.schema.json) | A Confluence page or blogpost. |
| `fireflies` | Fireflies | `/fireflies/graphql` | GraphQL (one `POST`) | [`fireflies.schema.json`](../backlot/schemas/fireflies.schema.json) | A Fireflies.ai meeting transcript. |
| `github` | GitHub | `/github` | 29 | [`github.schema.json`](../backlot/schemas/github.schema.json) | A GitHub issue or pull request. |
| `gmail` | Gmail | `/gmail/v1` | 8 | [`gmail.schema.json`](../backlot/schemas/gmail.schema.json) | A Gmail message. |
| `google_drive` | Google Drive, Docs, Sheets, Slides | `/drive/v3` `/docs/v1` `/sheets/v4` `/slides/v1` | 11 | [`google_drive.schema.json`](../backlot/schemas/google_drive.schema.json) | A Google Drive file. |
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
| `content` | |
| `content/{id}` | |
| `content/{id}/child/comment` | |
| `content/{id}/child/page` | |
| `content/{id}/label` | |
| `content/{id}/restriction/byOperation` | |
| `search` | CQL |
| `space` | |
| `space/{key}` | |
| `space/{key}/permission` | |

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
| `orgs/{org}/repos` | |
| `orgs/{org}/teams` | |
| `user/repos` | The token's own reach |
| `repos/{o}/{r}` | |
| `repos/{o}/{r}/issues[/{n}]` | |
| `repos/{o}/{r}/issues/{n}/comments` | |
| `repos/{o}/{r}/issues/comments/{id}` | |
| `repos/{o}/{r}/pulls[/{n}]` | |
| `repos/{o}/{r}/pulls/{n}/reviews` | |
| `repos/{o}/{r}/pulls/{n}/comments` | |
| `repos/{o}/{r}/pulls/{n}/files` | |
| `repos/{o}/{r}/pulls/{n}/commits` | |
| `repos/{o}/{r}/pulls/comments/{id}` | |
| `repos/{o}/{r}/readme` | |
| `repos/{o}/{r}/contents[/{path}]` | |
| `repos/{o}/{r}/git/trees/{ref}` | |
| `repos/{o}/{r}/git/blobs/{sha}` | |
| `repos/{o}/{r}/git/ref/{ref}` | Takes the ref as a trailing path, so `heads/release/2026-03` resolves |
| `repos/{o}/{r}/branches/{branch}` | |
| `repos/{o}/{r}/commits/{sha}` | |
| `repos/{o}/{r}/statuses/{sha}` | |
| `repos/{o}/{r}/collaborators` | |
| `repos/{o}/{r}/teams` | |

**Media types are honoured.** `Accept: application/vnd.github.raw` on `contents`/`readme`/`git/blobs`
returns the file's bytes; `…diff`/`…patch` on a pull returns a real unified diff / `git am` mbox; and
`…text-match+json` on `search/code` adds each hit's `text_matches` fragment.

**`X-GitHub-Api-Version` is honoured too**, in both values real currently supports: `2026-03-10`
drops `assignee` (issues and pulls) and `merge_commit_sha` (pulls), `2022-11-28` keeps them, an
unpinned request gets `2022-11-28`, anything else is the real API's 400, and every response reports
its choice in `X-GitHub-Api-Version-Selected`.

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
| `/drive/v3/files` | `q`: `fullText contains`, `name contains`, `mimeType`, `… in parents` incl. `'root'`, `trashed`, `modifiedTime`, `sharedWithMe`, `… in owners`. `orderBy`: `name`/`name_natural`/`createdTime`/`modifiedTime`/`recency`/`folder`/`starred`/`quotaBytesUsed`/`sharedWithMeTime` (+` desc`). `fields` projection, validated |
| `/drive/v3/files/{id}` | `fields` |
| `/drive/v3/files/{id}/export` | |
| `/drive/v3/files/{id}/permissions` | |
| `/drive/v3/drives` | |
| `/drive/v3/about` | `fields` **required**, as in real Drive; `storageQuota` is measured from the caller's visible corpus |
| `/docs/v1/documents/{id}` | |
| `/sheets/v4/spreadsheets/{id}` | Structure only — cells need `includeGridData=true` (+ optional `ranges`), as in real Sheets |
| `/sheets/v4/spreadsheets/{id}/values/{range}` | A1 ranges incl. `Sheet1!A1:B2`, `A:A`, `1:3`, `A2:B`, a bare sheet name quoted or not; `majorDimension`, `valueRenderOption` |
| `/sheets/v4/spreadsheets/{id}/values:batchGet` | As above |
| `/slides/v1/presentations/{id}` | |

The three editor APIs serve native-doc content for editor-aware clients, read structurally instead
of via Drive export.

Folders are files here: they match `mimeType='…folder'`, project, sort and resolve permissions like
stored rows. Trashed files are excluded unless `trashed = true` asks for them.

A spreadsheet row is one stored **line**, held in a single cell verbatim — Backlot picks no column
delimiter, so splitting (CSV, pipes, …) stays the corpus owner's decision. Reading a file of the
wrong type through any of the three editor APIs is refused, as real Google does, not reinterpreted.

#### OAuth and batch

Two more Google-shaped routes, both at the **server root** rather than under the prefixes above,
because that is where Google puts them.

| Endpoint | Notes |
|---|---|
| `POST /oauth2/token` | Turns a Google-style client credential into a bearer token the rest of Backlot already understands. Two grants: `refresh_token`, where the refresh token *is* the user's token from `/_meta/users`, and a signed service-account JWT assertion, whose `sub` claim selects the impersonated user under domain-wide delegation. A bare service account with no `sub` resolves to the admin/service identity. Expiry is cosmetic — a re-refresh returns the same token, so a long crawl never breaks |
| `POST /batch`, `POST /batch/{api}/{version}` | Google's `multipart/mixed` batch envelope: each part is an `application/http` sub-request, answered in order with its `Content-ID` preserved. The outer credential applies to any sub-request that does not carry its own, as real Google does |

`/batch` is Google-shaped but not Google-scoped — sub-requests are dispatched against the whole
app, so a batch may target any endpoint this server serves, not only Drive's.

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
| `issue/{key}/comment` | |
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

| Endpoint | Notes |
|---|---|
| `POST search` | |
| `pages/{id}` | |
| `blocks/{id}` | |
| `blocks/{id}/children` | |
| `databases/{id}` | Version-aware |
| `POST databases/{id}/query` | Legacy |
| `data_sources/{id}` | |
| `POST data_sources/{id}/query` | |
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
| `ListObjectsV2` | `prefix`, `delimiter`, `continuation-token` |
| `GetObject` | `Range` |
| `HeadObject` | |

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
| `/_meta/users` | Every generated user with their token and groups, in `data/tokens.yaml`'s shape, plus an `s3_access_key_id` / `s3_secret_access_key` pair each, since S3 authenticates with SigV4 rather than a bearer token. Pick a token, send it to any service, and see that user's ACL-filtered view |
| `/_meta/credentials` | The shared Google-style OAuth client and the org service account, for connectors that configure with an OAuth client instead of a raw token. No per-user data — a user's refresh token is their bearer token from `/_meta/users` |
| `/_meta/openapi/{source}` | One source's slice of `/openapi.json`, with the GET/POST and Jira v2/v3 fidelity aliases collapsed to one operation each, ready to hand to `FastMCP.from_openapi()`. S3 is absent by design: SigV4 signs each request, which a static `Authorization` header cannot do |
| `/openapi.json` | FastAPI's own typed spec for the whole server |

`/_meta/users` and `/_meta/credentials` hand out working credentials in the clear, so both 404 when
`BACKLOT_EXPOSE_TOKENS=false` — see [auth.md](auth.md) and [configuration.md](configuration.md).
