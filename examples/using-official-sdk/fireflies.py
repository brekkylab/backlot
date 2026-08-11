#!/usr/bin/env python3
"""Read Fireflies.ai meeting transcripts over raw HTTP. Self-contained: run it directly.

    pip install -e ".[examples]"
    python examples/using-official-sdk/fireflies.py          # or: --url http://localhost:8000
    python examples/using-official-sdk/fireflies.py --url http://localhost:8000 --token <usr-token>

**There is no Fireflies SDK, and raw HTTP is the official path.** The vendor's own quickstart
shows four examples — curl, Python ``requests.post``, JS ``axios.post`` and Java ``HttpClient`` —
all posting a GraphQL document to one endpoint with a Bearer key. So this script is not working
around a missing client: it is the documented way to use the API, and the base URL is just a
variable in user code. Nothing to shim. (Contrast ``linear/``, where the vendor DOES publish a
client and pointing it at the mock takes a real shim.)

``httpx`` is already a dependency, so that is what this uses; ``requests`` would be identical.
"""

import argparse
import json

import httpx
from backlot import serve_or_connect

# Two meetings in one channel. The second supplies only a `content` body — a plain
# "Speaker: text" transcript — to show that the loader parses sentences back out of it, so a BYO
# corpus does not have to be written in the structured form.
CORPUS = [
    {
        "source_type": "fireflies",
        "channel": "sales-calls",
        "doc_id": "ff-ex-discovery",
        "title": "Acme Health — latency discovery",
        "host_email": "rep@acme.com",
        "host_name": "Dana Rep",
        "duration": 34.0,
        "created": "2026-04-02T15:00:00Z",
        "summary": {
            "overview": "Acme Health needs sub-300ms p95 before they will pilot.",
            "topics_discussed": ["latency budget", "batching", "EU residency"],
            "action_items": [
                "Dana: send the batching benchmark",
                "Ivan: confirm EU data residency",
            ],
            "keywords": ["latency", "batching", "residency"],
            "meeting_type": "discovery",
        },
        "meeting_attendees": [
            {"displayName": "Dana Rep", "email": "rep@acme.com", "location": None},
            {
                "displayName": "Ivan Ortiz",
                "email": "ivan@acme-health.example",
                "location": "Acme Health",
            },
        ],
        "sentences": [
            {
                "speaker_name": "Dana Rep",
                "author_email": "rep@acme.com",
                "start_time": 0,
                "text": "Thanks for making time — let's start with the latency budget.",
            },
            {
                "speaker_name": "Ivan Ortiz",
                "start_time": 18,
                "text": "Our p95 sits around 300 milliseconds and batching is the suspect.",
            },
            {
                "speaker_name": "Dana Rep",
                "author_email": "rep@acme.com",
                "start_time": 41,
                "text": "Understood. I'll send the batching benchmark right after this.",
            },
            {
                "speaker_name": "Ivan Ortiz",
                "start_time": 63,
                "text": "One more thing — we need EU data residency confirmed in writing.",
            },
            {"speaker_name": None, "start_time": 79, "text": "(crosstalk)"},
        ],
    },
    {
        "source_type": "fireflies",
        "channel": "sales-calls",
        "doc_id": "ff-ex-checkin",
        "title": "Acme Health — pilot check-in",
        "host_email": "rep@acme.com",
        "duration": 21.0,
        "created": "2026-04-16T15:00:00Z",
        "content": "[00:00] Dana: quick check-in on the pilot numbers.\n"
        "[00:24] Ivan: p95 is down to 240 milliseconds with batching on.\n"
        "We're comfortable moving to the security review.\n"
        "[01:02] Dana: great — I'll get the questionnaire over today.",
    },
]

# The vendor quickstart's own shape: one POST, a `query` (plus `variables`), a Bearer key.
TRANSCRIPTS = """
query Transcripts($limit: Int, $skip: Int, $keyword: String, $scope: String) {
  transcripts(limit: $limit, skip: $skip, keyword: $keyword, scope: $scope) {
    id
    title
    dateString
    duration
    host_email
    organizer_email
    channels
    participants
    transcript_url
    audio_url
    summary { overview topics_discussed action_items keywords meeting_type }
    analytics {
      sentiments { positive_pct neutral_pct negative_pct }
      speakers { name duration word_count duration_pct }
    }
    meeting_attendees { displayName email location }
  }
}
"""

ONE_TRANSCRIPT = """
query Transcript($id: String!) {
  transcript(id: $id) {
    id
    title
    duration
    sentences { index speaker_name speaker_id text start_time end_time }
  }
}
"""

parser = argparse.ArgumentParser()
parser.add_argument("--url", help="existing mock server (default: spawn a throwaway one)")
parser.add_argument("--token", help="user token; responses are then ACL-filtered to that user")
args = parser.parse_args()


def gql(client, endpoint, token, query, **variables):
    """One raw POST, exactly as the vendor's quickstart does it."""
    r = client.post(
        endpoint,
        json={"query": query, "variables": variables},
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
    )
    r.raise_for_status()
    body = r.json()
    # A GraphQL 200 can still carry `errors` alongside partial data — always check.
    if body.get("errors"):
        raise SystemExit(f"GraphQL errors: {json.dumps(body['errors'], indent=2)}")
    return body["data"]


with serve_or_connect(CORPUS, url=args.url) as mock:
    if args.token:
        print("authenticating with --token → responses are ACL-filtered to that user")
    token = args.token or mock.token
    endpoint = f"{mock.base_url}/fireflies/graphql"

    with httpx.Client(timeout=30) as client:
        data = gql(client, endpoint, token, TRANSCRIPTS, limit=10)
        print(f"transcripts → {len(data['transcripts'])}")
        for t in data["transcripts"]:
            s = t["summary"]
            print(f"\n  {t['title']}")
            print(f"    {t['dateString']}  {t['duration']} min  host={t['host_email']}")
            print(f"    channels={t['channels']}  id={t['id']}")
            print(f"    overview: {(s['overview'] or '(none)')[:88]}")
            if s["topics_discussed"]:
                print(f"    topics: {', '.join(s['topics_discussed'])}")
            if s["action_items"]:
                # action_items is ONE newline-joined string in the real API, not a list
                for item in s["action_items"].split("\n"):
                    print(f"      [ ] {item}")
            sent = t["analytics"]["sentiments"]
            print(
                f"    sentiment: +{sent['positive_pct']}% /{sent['neutral_pct']}% "
                f"-{sent['negative_pct']}%"
            )
            for spk in t["analytics"]["speakers"]:
                print(
                    f"      {spk['name'] or '(unattributed)'}: {spk['duration']}s "
                    f"({spk['duration_pct']}%), {spk['word_count']} words"
                )
            for a in t["meeting_attendees"]:
                where = f" @ {a['location']}" if a["location"] else ""
                print(f"      attendee: {a['displayName']} <{a['email']}>{where}")

        # `limit` is capped at 50 by the real API. It CLAMPS rather than erroring, so asking for
        # more is safe — you just get 50.
        big = gql(client, endpoint, token, TRANSCRIPTS, limit=500)
        print(f"\nlimit=500 → served {len(big['transcripts'])} (the API's max is 50)")

        # Offset pagination: `limit`/`skip`, NOT a Relay cursor connection.
        page = gql(client, endpoint, token, TRANSCRIPTS, limit=1, skip=1)
        print(f"limit=1 skip=1 → {page['transcripts'][0]['title']}")

        # `keyword` is scoped by `scope`: title | sentences | all.
        for scope in ("title", "sentences", "all"):
            hits = gql(
                client, endpoint, token, TRANSCRIPTS, limit=50, keyword="batching", scope=scope
            )["transcripts"]
            print(
                f"keyword='batching' scope={scope:<10} → {len(hits)} {[h['title'] for h in hits]}"
            )

        # Sentences: the transcript's utterances, with per-speaker timings in SECONDS
        # (`duration` above is in MINUTES — the two units really do differ in this API).
        first = data["transcripts"][0]["id"]
        one = gql(client, endpoint, token, ONE_TRANSCRIPT, id=first)["transcript"]
        print(f"\ntranscript({first}) → {one['title']}")
        for s in one["sentences"]:
            who = s["speaker_name"] or "(unattributed)"
            print(
                f"  [{s['start_time']:6.1f}s-{s['end_time']:6.1f}s] "
                f"#{s['speaker_id']} {who}: {s['text']}"
            )

        # The concatenated sentence text IS the document the mock indexes for search, so the
        # transcript round-trips: joining the sentences back up recovers it exactly.
        rebuilt = "\n".join(
            f"{s['speaker_name']}: {s['text']}" if s["speaker_name"] else s["text"]
            for s in one["sentences"]
        )
        print(f"\nrebuilt transcript ({len(rebuilt)} chars) — this is what full-text search sees:")
        print("  " + rebuilt.replace("\n", "\n  "))
