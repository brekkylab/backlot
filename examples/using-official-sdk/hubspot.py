#!/usr/bin/env python3
"""Read HubSpot CRM through the official hubspot-api-client SDK. Self-contained: run it directly.

    pip install -e ".[examples]"
    python examples/using-official-sdk/hubspot.py            # or: --url http://localhost:8000
    python examples/using-official-sdk/hubspot.py --url http://localhost:8000 --token <usr-token>

The only change from talking to real HubSpot is ``host`` — a plain constructor argument, because
the client's ``_default_api_factory`` copies every unknown kwarg onto its ``Configuration``. No
patching or shim is needed, which makes this the simplest of the SDK examples.

Requires the current SDK (>=12). On 8.x the ``host`` kwarg is silently **ignored** and the client
talks to api.hubapi.com instead — so ``assert_reaches_mock`` below fails loudly rather than letting
an example quietly hit production.
"""

import argparse
import sys
from pathlib import Path

from backlot import serve_or_connect

# This file is named hubspot.py, so its own directory would shadow the SDK's `hubspot` package.
# Drop that directory now that the local helper is imported (same as github.py).
_here = str(Path(__file__).resolve().parent)
sys.path[:] = [p for p in sys.path if p and Path(p).resolve() != Path(_here)]

from hubspot import HubSpot  # noqa: E402
from hubspot.crm.companies import PublicObjectSearchRequest  # noqa: E402

# The object type is the CRM's grouping unit here, so these records span four of them. An
# association is declared once; the loader materializes the reverse direction too.
CORPUS = [
    {
        "source_type": "hubspot",
        "object_type": "companies",
        "doc_id": "hs-co-acme",
        "title": "Acme Health",
        "content": "Mid-market healthcare provider evaluating the platform.",
        "author_email": "rep@acme.com",
        "properties": {
            "name": "Acme Health",
            "domain": "acme-health.com",
            "industry": "healthcare",
            "lifecyclestage": "evaluation",
            "hq_region": "eu",
        },
    },
    {
        "source_type": "hubspot",
        "object_type": "contacts",
        "title": "Ava Stone",
        "content": "Ava Stone — VP Platform at Acme Health.",
        "author_email": "rep@acme.com",
        "properties": {
            "firstname": "Ava",
            "lastname": "Stone",
            "email": "ava@acme-health.com",
            "jobtitle": "VP Platform",
        },
        "associations": [{"to": "hs-co-acme", "label": "Primary"}],
    },
    {
        "source_type": "hubspot",
        "object_type": "deals",
        "title": "Acme Health — renewal",
        "content": "12-month renewal, EU residency required.",
        "author_email": "rep@acme.com",
        "properties": {
            "dealname": "Acme Health — renewal",
            "amount": "50000",
            "dealstage": "contractsent",
        },
        "associations": [{"to": "hs-co-acme"}],
    },
    {
        "source_type": "hubspot",
        "object_type": "notes",
        "title": "",
        "content": "Security review scheduled; customer wants EU data residency confirmed.",
        "author_email": "rep@acme.com",
        "properties": {
            "hs_note_body": "Security review scheduled; customer wants EU data residency confirmed."
        },
        "associations": [{"to": "hs-co-acme"}],
    },
]

_p = argparse.ArgumentParser(
    description="Read HubSpot CRM through the official hubspot-api-client SDK against the mock."
)
_p.add_argument("--url", help="mock base URL to drive (default: spin up a local throwaway mock)")
_p.add_argument(
    "--token",
    help="mock bearer token from GET /_mock/users "
    "(default: the admin token, which sees everything)",
)
args = _p.parse_args()


def assert_reaches_mock(api, base_url: str) -> None:
    """Fail loudly if `host` did not take effect — an ignored override would send this example's
    traffic to the real HubSpot API, which looks like an auth error rather than a config bug."""
    host = api.crm.companies.basic_api.api_client.configuration.host
    if base_url not in host:
        raise SystemExit(
            f"the SDK is configured for {host!r}, not the mock at {base_url!r} — the `host` kwarg "
            f"was ignored. Upgrade: pip install -U 'hubspot-api-client>=12'"
        )


with serve_or_connect(CORPUS, url=args.url) as mock:
    if args.token:
        print("authenticating with --token → responses are ACL-filtered to that user")
    api = HubSpot(access_token=args.token or mock.token, host=f"{mock.base_url}/hubspot")
    assert_reaches_mock(api, mock.base_url)

    # get_all pages until a response omits paging.next — the mock's termination contract
    companies = api.crm.companies.get_all()
    print(f"companies → {len(companies)}")
    for c in companies:
        p = c.properties
        print(
            f"  - {p.get('name')} [{p.get('lifecyclestage')}] {p.get('domain')} "
            f"industry={p.get('industry')}"
        )

    contacts = api.crm.contacts.get_all()
    print(f"\ncontacts → {len(contacts)}")
    for c in contacts:
        p = c.properties
        print(
            f"  - {p.get('firstname')} {p.get('lastname')} <{p.get('email')}> {p.get('jobtitle')}"
        )

    deals = api.crm.deals.get_all()
    print(f"\ndeals → {len(deals)}")
    for d in deals:
        print(
            f"  - {d.properties.get('dealname')} ${d.properties.get('amount')} "
            f"[{d.properties.get('dealstage')}]"
        )

    # search: filterGroups are OR-ed, the filters inside one group AND-ed
    req = PublicObjectSearchRequest(
        filter_groups=[
            {
                "filters": [
                    {"propertyName": "industry", "operator": "EQ", "value": "healthcare"},
                    {"propertyName": "hq_region", "operator": "EQ", "value": "eu"},
                ]
            }
        ]
    )
    found = api.crm.companies.search_api.do_search(public_object_search_request=req)
    print(f"\nsearch industry=healthcare AND hq_region=eu → total={found.total}")
    for r in found.results:
        print(f"  - {r.properties.get('name')}")

    # associations (v4): what hangs off the company
    company_id = companies[0].id
    for to_type in ("contacts", "deals", "notes"):
        assoc = api.crm.associations.v4.basic_api.get_page(
            object_type="companies", object_id=company_id, to_object_type=to_type
        )
        print(f"\ncompany {company_id} → {to_type}: {len(assoc.results)}")
        for a in assoc.results:
            label = a.association_types[0].label or "-"
            print(f"  - id={a.to_object_id} label={label}")
