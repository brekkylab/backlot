#!/usr/bin/env python3
"""Read Microsoft Teams through the official msgraph-sdk. Self-contained: run it directly.

    pip install -e ".[official-sdk]"
    python examples/using-official-sdk/msteams.py          # or: --url http://localhost:8000
    python examples/using-official-sdk/msteams.py --url http://localhost:8000 --token <usr-token>

Pointing the SDK at Backlot takes two small pieces, and both are below with the reason beside them:
a credential that hands over a token Backlot already issued, and a base URL override. The SDK is
async-only, so this script is too; every other example here is synchronous.
"""

import argparse
import asyncio

from azure.core.credentials import AccessToken
from kiota_abstractions.base_request_configuration import RequestConfiguration
from kiota_authentication_azure.azure_identity_authentication_provider import (
    AzureIdentityAuthenticationProvider,
)
from msgraph import GraphServiceClient
from msgraph.generated.teams.item.channels.item.messages.messages_request_builder import (
    MessagesRequestBuilder,
)
from msgraph.graph_request_adapter import GraphRequestAdapter

from backlot import serve_or_connect

CORPUS = [
    {
        "source_type": "msteams",
        "channel": "Platform",
        "author_email": "ava@acme.com",
        "created": "2026-02-05T17:00:00Z",
        "subject": "Deploy freeze",
        "content": "Deploy freeze starts Friday 5pm.",
    },
    {
        "source_type": "msteams",
        "channel": "Incidents",
        "author_email": "bob@acme.com",
        "created": "2026-02-10T18:00:00Z",
        "content": "Anyone seeing 502s from the gateway?",
        "replies": [
            {
                "author_email": "ava@acme.com",
                "created": "2026-02-10T18:00:40Z",
                "content": "Looking now.",
            },
            {
                "author_email": "bob@acme.com",
                "created": "2026-02-10T18:06:00Z",
                "content": "Rolled back — clearing up.",
            },
        ],
    },
]


class BacklotCredential:
    """The whole adapter between Backlot's tokens and the SDK.

    ``GraphServiceClient`` expects an azure-identity credential, whose one obligation is to answer
    ``get_token`` with an ``AccessToken``. Backlot already issued the token (``/_meta/users``), so
    there is no sign-in flow to reproduce: the credential just hands the same string back for
    whatever scopes it is asked about. Nothing here reaches Microsoft Entra.
    """

    def __init__(self, token: str):
        self._token = token

    def get_token(self, *scopes, **kwargs) -> AccessToken:
        # The far future, because Backlot's tokens do not expire and a past expiry would make the
        # SDK's own refresh logic fire against a flow that does not exist here.
        return AccessToken(self._token, 2**31 - 1)


def client_at(base_url: str, token: str) -> GraphServiceClient:
    """A ``GraphServiceClient`` pointed at Backlot instead of graph.microsoft.com.

    ``GraphServiceClient(credentials=…)`` builds its own request adapter against
    ``https://graph.microsoft.com``, and that host is baked into the client factory rather than
    exposed as an argument. Constructing the adapter and setting ``base_url`` on it is the seam:
    the generated request builders below append their paths to whatever it holds.

    Plain ``http`` to ``127.0.0.1`` needs no further configuration. The auth provider's
    ``allowed_hosts`` defaults to empty, which it reads as "every host is allowed", so it attaches
    the token here as readily as it would to the real service.
    """
    auth = AzureIdentityAuthenticationProvider(BacklotCredential(token), scopes=["/.default"])
    adapter = GraphRequestAdapter(auth)
    adapter.base_url = f"{base_url.rstrip('/')}/msgraph/v1.0"
    return GraphServiceClient(request_adapter=adapter)


async def read(graph: GraphServiceClient) -> None:
    me = await graph.me.get()
    print(f"signed in as {me.display_name} <{me.user_principal_name}>")

    teams = await graph.me.joined_teams.get()
    team = graph.teams.by_team_id(teams.value[0].id)
    channels = await team.channels.get()
    if not channels.value:
        print("no channels visible to this identity")
        return

    print(f"{teams.value[0].display_name}: {len(channels.value)} channels")
    for channel in channels.value:
        messages = team.channels.by_channel_id(channel.id)
        # `$expand=replies` folds each post's replies into the same response, which is what saves a
        # crawler a request per thread. The generated query-parameter class is how the SDK spells
        # an OData option.
        config = RequestConfiguration(
            query_parameters=MessagesRequestBuilder.MessagesRequestBuilderGetQueryParameters(
                expand=["replies"]
            )
        )
        posts = await messages.messages.get(request_configuration=config)
        print(f"#{channel.display_name} ({channel.membership_type}) has {posts.odata_count} posts:")
        for post in posts.value:
            sender = post.from_.user.display_name if post.from_ else "system"
            print(f"  - [{sender}] {post.body.content.splitlines()[0]}")
            for reply in post.replies or []:
                who = reply.from_.user.display_name if reply.from_ else "system"
                print(f"      ↳ [{who}] {reply.body.content.splitlines()[0]}")

        members = await messages.members.get()
        print(f"  members: {', '.join(m.display_name for m in members.value)}")


_p = argparse.ArgumentParser(
    description="Read Microsoft Teams through the official msgraph-sdk against Backlot."
)
_p.add_argument(
    "--url", help="Backlot base URL to drive (default: spin up a local throwaway server)"
)
_p.add_argument(
    "--token",
    help="Backlot bearer token from GET /_meta/users "
    "(default: the admin token, which sees everything)",
)
args = _p.parse_args()

with serve_or_connect(CORPUS, url=args.url) as s:
    if args.token:
        print("authenticating with --token → responses are ACL-filtered to that user")
    asyncio.run(read(client_at(s.base_url, args.token or s.token)))
