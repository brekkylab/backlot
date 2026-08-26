---
name: Fidelity gap
about: Backlot answers something the real vendor API does not
title: "source: "
labels: ''
---

<!-- The common issue here. A divergence is a bug even when the mock's answer is reasonable, so
     what a report needs is both answers and the measurement that separates them. One pass
     usually finds several (#23 is seven, #49 eight): state each in its own block below, with a
     summary table up front once there is more than one. Delete the prompts as you fill them in. -->

**Source and commit** — `slack`, `gmail`, … and the Backlot version or commit you saw this at.

**How you know** — the measurement and its date: a live call, an introspected schema, a client
generated from the vendor's schema, a vendor SDK that reads the response differently. Vendor docs
are worth citing, but they have contradicted a live schema before now (#68), so say if docs are
all you have.

**Reproduction** — the corpus it was served over (the JSONL records, and the roster if membership
or ACLs are involved) and how you pointed a client at it. For a mock served over a corpus you
supply, those records *are* the repro.

<!-- With more than one divergence, open with the table — which of them are silent is what sets
     the order they get fixed in:

     | # | Divergence | Silent? |
     |---|---|---|
     | 1 | `q: sharedWithMe` clause ignored | yes | -->

## 1. <the divergence, in a sentence>

**Request**

**Backlot serves**

**The real API serves**

**Can a client tell?** A `200` with a wrong body is the expensive case: nothing downstream notices
until it runs against the real service.
