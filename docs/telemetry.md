# Usage telemetry

Backlot is only worth using where it answers the way the real API does, and every vendor publishes far more API than Backlot serves today. We would rather put the work where people already are than guess at it. So a running Backlot server counts which vendor operations it is asked for, and sends those counts to us.

## Why we collect it

The counts answer three questions, and the answers set the order of work on Backlot:

- **Which sources and operations do people use?** Those are measured against the real API and fixed first.
- **What do people ask for that Backlot does not serve yet?** A call to an operation Backlot lacks is counted under that operation's own name, so the next operations built are the ones people reached for.
- **Where do answers fail?** An internal error, or a filter Backlot cannot evaluate yet, points at what to fix next.

That is the whole use of it. In return, the parts of Backlot you use are the parts that get better first. If you would rather not take part, turning it off is one flag, below.

## What it is not

It is anonymous and coarse by design. It counts calls, never callers. The counts go to `https://telemetry.brekkylab.com/v1/metrics` once a minute and when the server stops.

## Turning it off

| How | Where |
|---|---|
| `backlot serve --disable-telemetry` | The CLI |
| `backlot mcp --disable-telemetry` | The MCP bridge, for the server it starts when none is running |
| `backlot.serve(telemetry=False)`, `backlot.serve_or_connect(telemetry=False)` | Python, for the server it starts |
| `BACKLOT_TELEMETRY=false` | The environment, for any server started from it, Docker included |
| `DO_NOT_TRACK=1` | The environment or `.env`, as for `BACKLOT_TELEMETRY`: the convention many developer tools honour |

Turn it off wherever Backlot runs for something other than your own use of it: a test suite, a CI job, a benchmark harness. A server that sends telemetry says so on stderr when it starts.

## What is sent

Two OTLP metrics, as delta counts over each interval:

- `backlot.http.requests`: calls.
- `backlot.http.duration`: how long Backlot took to answer, as a histogram in milliseconds.

A server keeps at most 4096 combinations of attributes per interval. Calls past that are not counted under their attributes, and a third count, `backlot.http.dropped`, says how many there were.

With them goes one gauge, `backlot.surface.operations`: for each source and support state, how many of the vendor's operations this version of Backlot's baselines records. It is the same for every server running that version, and it is what coverage is measured against.

Each count carries these attributes:

| Attribute | Value |
|---|---|
| `backlot.source` | The source whose API the path belongs to: `github`, `slack`, `gmail`, `google_drive`, `jira`, `confluence`, `notion`, `s3`, `hubspot`, `linear`, `fireflies`, `msteams`, or `other` for a path under none |
| `backlot.operation` | The vendor operation, by the vendor's own id, e.g. `repos/list-commits`, `conversations.history`, `ListObjectsV2`, `query.issues` |
| `backlot.support` | `implemented`, `extra` (a route the vendor's contract does not describe), `refused` (routed only to be refused, as S3 writes are), `missing` (the vendor has it and this Backlot does not), or `unknown` |
| `http.request.method` | `GET`, `POST`, … |
| `http.response.status_code` | The status Backlot answered with |
| `backlot.error_code` | The vendor's error identifier when the answer is an error, e.g. `channel_not_found`, `NoSuchKey`, `not_found` |
| `backlot.error_class` | On a failure, its cause, read from whether Backlot itself failed, the status, the support state, the error code and a spent rate-limit header: `unsupported` (Backlot does not do this yet), `internal` (a Backlot bug), `rate_limit`, `auth`, or `client` (the request was wrong) |
| `backlot.auth.kind` | `bearer`, `basic`, `api_key`, `sigv4`, `sigv2`, `presigned`, `other` or `anonymous` |
| `backlot.caller.kind` | `admin`, `user` or `anonymous` |
| `backlot.client` | The client library the request headers name, by name and, where the headers carry one, major.minor version, e.g. `slack_sdk/3.44`, `boto3/1.40`, `python-requests/2.34`, or `other` |

Each export carries the Backlot version, the operating system family (`darwin`, `linux`, `windows`) and the Python version.

## What is never sent

- Paths, query strings and request or response bodies.
- GraphQL documents, variables, aliases or argument values.
- Error messages. An error is sent as its fixed code only.
- Tokens, keys, emails and user names.
- Anything from your corpus: channel, repository, bucket, key, page or issue names and ids.
- Any identifier of the installation, the machine or the person, and no `User-Agent` string as sent.

An export arrives from your network address, as any request does. The receiving side does not keep it: the collector stores only the values listed above, and the load balancer in front of it keeps no access log.

An operation's name comes from Backlot's own fidelity baselines (`backlot/fidelity/baseline/`), which record every operation of each vendor's contract, or, for a route Backlot serves beyond that contract (`extra`), from the route's path template, such as `wiki/rest/api/content/{}`, never from the request. A Google batch envelope is `batch`, beside a call for each of its parts. For the `github`, `hubspot`, `gmail` and `google_drive` sources, a request to a path Backlot serves, under a method neither Backlot nor the vendor's contract has there, is counted under a path template with support `unknown`, such as `crm/v3/objects/{}/{}` for a `PUT` to a record: the route's, or the vendor template with more literal segments where one fits the path under another method (`crm/v3/objects/{}/search` for an `OPTIONS` before a search). A template holding a character an operation name cannot (the `:` of Sheets' `values:batchGet`) is not used. Any other request that asks for something no baseline names is counted as `unmatched`, a GraphQL field outside the vendor's schema as `query.unmatched`, a GraphQL document that does not parse as `unparsed`, and a mutation or subscription as `mutation` or `subscription`.

## The code

`backlot/telemetry/` is all of it: the middleware that counts, the naming, and the exporter. `tests/test_telemetry.py` holds it to this page, including a test that a marker placed in every part of a request never reaches an export.
