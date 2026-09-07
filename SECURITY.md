# Security Policy

## Reporting a vulnerability

Report privately through [GitHub's advisory form](https://github.com/brekkylab/backlot/security/advisories/new),
or by email to oceanjoon@brekkylab.com. Please do not open a public issue for a suspected
vulnerability.

Include what you ran, what you expected, and what happened. A reproduction against the bundled
corpus (`backlot import --bundled`) is ideal, because it is the corpus we can both look at.

We aim to acknowledge a report within three working days and to describe the fix or the
disagreement within fourteen. Fixes ship in a normal release, credited to the reporter unless they
ask otherwise.

Only the latest release on PyPI is supported.

## What counts

Backlot serves fabricated documents to a caller on localhost, so the interesting failures are the
ones that break the boundaries it claims to enforce:

- **A document reaching a caller who should not read it.** Every user in the corpus gets a token,
  and a source's access rules decide what each token can see. A response that leaks a document,
  its metadata, or its existence across that line is a vulnerability, not a fidelity gap.
- **A request escaping the corpus.** Path traversal out of the served files, SQL injected through
  a query parameter, or a route that reads or writes outside the SQLite corpus and the directories
  the CLI was pointed at.
- **Code execution from data.** A corpus record, a BYO JSONL file, or an import artifact that runs
  as code when Backlot loads it.
- **The importer or the CLI writing outside their arguments**, including through an archive entry
  or a filename in the corpus.

## What does not

- **The credentials in this repository are not secrets.** `admin-service-token`, the bot token in
  `tests/test_slack.py`, the `AKIA…BOGUS` access keys in `tests/test_s3.py`, and every per-user
  token the importer mints authenticate against a local corpus and nothing else. They are the
  product, not a leak.
- **Binding and exposure are the operator's call.** `backlot serve` is a development server; the
  defaults assume localhost. Putting one on a public interface is a deployment decision, and its
  consequences are not a vulnerability in Backlot.
- **The data is invented.** Names, addresses and documents in the corpora resemble real ones by
  design; none describe a real person.
- **A response that diverges from the vendor's** is a fidelity bug. Those belong in a public issue
  — unless the divergence is one of the boundaries above.
