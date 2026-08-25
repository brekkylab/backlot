---
name: Fidelity gap
about: Backlot answers something the real vendor API does not
title: "source: "
labels: ''
---

<!-- The most useful issue in this repo. A divergence is a bug even when the mock's answer is
     reasonable, so the shape of the report is: what we serve, what the vendor serves, how you
     know. Delete the prompts as you fill them in. -->

**Source and request** — the endpoint or GraphQL operation, and the call that shows it.

**Backlot serves**

**The real API serves**

**How you know** — the measurement, and the date and commit you made it at. A live call, an
introspected schema, a generated client that fails to bind, or a vendor SDK that reads the
response differently. Vendor docs are worth citing, but they have contradicted the live schema
before now; say so if docs are all you have.

**Can a client tell?** A `200` with the wrong body is worse than an error: nothing downstream
notices until it runs against the real service.
