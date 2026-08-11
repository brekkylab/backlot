#!/usr/bin/env python3
"""Load HubSpot CRM records through the official llama-index HubSpot reader. Self-contained.

HubspotReader takes only an access token and constructs the SDK client itself, so
point_hubspot_at() rebinds `hubspot.HubSpot` to inject the mock's host before the reader runs.

The reader is deliberately NOT in the [llamaindex] extra: it pins hubspot-api-client<9, which no
resolver can reconcile with the >=12 that [examples] needs. The pin is over-restrictive — the reader
only calls HubSpot(access_token=...) and crm.{deals,contacts,companies}.get_all(), all present in
12.x — so install it past its own pin:

    pip install -e ".[examples,llamaindex]"
    pip install --no-deps llama-index-readers-hubspot
    python examples/using-llamaindex-readers/hubspot.py            # or: --url http://localhost:8000
    python examples/using-llamaindex-readers/hubspot.py --url http://localhost:8000 --token <usr-token>

Note what the reader returns: **three** Documents — one each for deals, contacts, and companies —
whose text is the `str()` of a list of SDK objects, not one Document per record. That is the
reader's own design; the mock just serves the three listings it pages through.
"""

import argparse
import sys
from pathlib import Path

from backlot import serve_or_connect
from backlot.integrations.llamaindex import point_hubspot_at

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from _common.syspath import drop_self_from_syspath

# Named hubspot.py, so this directory would shadow the SDK's `hubspot` package for the reader's
# own `from hubspot import HubSpot`.
drop_self_from_syspath(__file__)

from llama_index.readers.hubspot import HubspotReader  # noqa: E402

CORPUS = [
    {
        "source_type": "hubspot",
        "object_type": "companies",
        "doc_id": "hs-co-acme",
        "title": "Acme Health",
        "content": "Mid-market healthcare provider evaluating the platform.",
        "properties": {
            "name": "Acme Health",
            "domain": "acme-health.com",
            "industry": "healthcare",
            "lifecyclestage": "evaluation",
        },
    },
    {
        "source_type": "hubspot",
        "object_type": "contacts",
        "title": "Ava Stone",
        "content": "Ava Stone — VP Platform at Acme Health.",
        "properties": {"firstname": "Ava", "lastname": "Stone", "email": "ava@acme-health.com"},
        "associations": [{"to": "hs-co-acme", "label": "Primary"}],
    },
    {
        "source_type": "hubspot",
        "object_type": "deals",
        "title": "Acme Health — renewal",
        "content": "12-month renewal, EU residency required.",
        "properties": {"dealname": "Acme Health — renewal", "amount": "50000"},
        "associations": [{"to": "hs-co-acme"}],
    },
]


def build(mock, token):
    point_hubspot_at(f"{mock.base_url}/hubspot")
    return HubspotReader(access_token=token)


def main(reader):
    # load_data() pages every object type to exhaustion via the SDK's fetch_all, which stops only
    # when a response omits paging.next — so this returning at all exercises that contract.
    docs = reader.load_data()
    print(f"loaded {len(docs)} Document(s):")
    for d in docs:
        kind = d.metadata.get("type")  # `extra_info` is the deprecated alias for this
        print(f"  - {kind}: {len(d.text)} chars")
        for needle in ("Acme Health", "Ava", "renewal"):
            if needle in d.text:
                print(f"      contains {needle!r}")


def _parse_args():
    p = argparse.ArgumentParser(description="Load HubSpot CRM via llama-index against the mock.")
    p.add_argument("--url", help="mock base URL (default: spin up a local throwaway mock)")
    p.add_argument("--token", help="mock bearer token from GET /_mock/users (default: admin)")
    return p.parse_args()


if __name__ == "__main__":
    args = _parse_args()
    with serve_or_connect(CORPUS, url=args.url) as mock:
        if args.token:
            print("authenticating with --token → responses are ACL-filtered to that user")
        main(build(mock, args.token or mock.token))
