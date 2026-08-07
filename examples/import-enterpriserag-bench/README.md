# Import from EnterpriseRAG-Bench

[EnterpriseRAG-Bench](https://github.com/onyx-dot-app/EnterpriseRAG-Bench) ships structured
``generated_data/`` — real owners/authors/dates/participants/ACL signals per doc, across **all
nine** of the sources it ships (Slack, Gmail, Linear, Google Drive, HubSpot, Fireflies, GitHub,
Jira, Confluence — 511,962 documents). One command downloads it, loads
it into the per-service tables, derives the ACL from the real people/scope fields, and writes
``tokens.yaml`` for the resolved roster:

```bash
python -m backlot.importer.erb                                           # full corpus: download -> load -> ACL
python -m backlot.importer.erb --slice-questions extra_questions.jsonl    # only the docs a slice needs
python -m backlot.importer.erb --no-download                              # reuse whatever is already in data/raw
python -m backlot.importer.erb --ref some-branch                          # fetch a non-default branch/ref
```

This is faithful representation, not synthesis: names are resolved to real emails via the
employee directory (``backlot.importer.principals``), and **every import parses the real
conversations embedded in the content** (Slack transcripts → threads, GitHub PR reviews / Jira
comments → real comments, Gmail threads → per-email messages, Fireflies transcripts →
per-sentence utterances with speakers and timings).

## Walkthrough

`run.py` runs the import into `examples/import-enterpriserag-bench/data` (downloading on the
first run; cached after), starts a real mock server against it, and prints what got served:

```bash
python examples/import-enterpriserag-bench/run.py                                   # full corpus
python examples/import-enterpriserag-bench/run.py --slice-questions extra_questions.jsonl  # a slice
```
