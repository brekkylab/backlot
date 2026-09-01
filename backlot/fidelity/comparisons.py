"""Where each source's two contracts come from: Backlot's own, and the vendor's published one.

Backlot's side reads the SDL file the server itself builds from — ``backlot.graphql.engine.from_sdl``
loads that same path at import — so the comparison is against what is served, without starting a
server or importing a corpus.

The vendor side needs a credential. That is the whole reason this runs on a schedule and not on
every pull request: a fork cannot see the token, and a vendor outage must not block a contributor.
The no-credential half of drift detection — vendor-published OpenAPI and Google Discovery documents
— is a separate surface and does not belong to this module.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any, Callable, Mapping
from pathlib import Path


from backlot.fidelity import (
    google_discovery_diff,
    graphql_diff,
    hubspot_catalog,
    openapi_diff,
    s3_probe,
)
from backlot.fidelity.errors import FidelityError
from backlot.fidelity.findings import Finding


@dataclass(frozen=True)
class Credential:
    """One secret a source needs, named twice: as the caller spells it and as the environment does.

    A tuple of these rather than a single token because a credential is not always one string.
    Fireflies wants an API key; a vendor authenticated with SigV4 wants an access key AND a secret,
    and a Google service account wants a client id, a client secret and a private key. A source
    that needs three says so, and the resolution below is the same for all of them.
    """

    name: str
    env: str
    what: str


@dataclass(frozen=True)
class OpenAPIComparison:
    """A source compared against the OpenAPI document its vendor publishes.

    Public, which is what lets this kind gate a pull request: no credential, no quota, no account.

    Most vendors publish the document at a stable URL. One does not, and ``resolve_url`` is for
    that: ``spec_url`` addresses an index, and the hook picks the entry out of it.
    """

    name: str
    spec_url: str
    mount: tuple[str, ...]
    strip: str = ""
    # Set when ``spec_url`` addresses an INDEX rather than the document itself: given what was
    # fetched, it returns the URL to fetch instead. A vendor that publishes one document per
    # release needs this — see `hubspot_catalog` for why pinning the resolved URL is
    # not the simplification it appears to be.
    resolve_url: Callable[[Mapping[str, Any]], str] | None = None
    credentials: tuple[Credential, ...] = ()

    @property
    def endpoint(self) -> str:
        return self.spec_url

    def divergences(
        self, credentials: Mapping[str, str] | None = None, *, timeout: float = 120.0
    ) -> list[Finding]:
        return openapi_diff.divergences(self, timeout=timeout)


@dataclass(frozen=True)
class GoogleDiscoveryComparison:
    """A source compared against the Google API Discovery document its vendor publishes.

    Its own class rather than a flag on the one above, because Discovery is not OpenAPI: methods
    nest under resources, paths join to a ``servicePath``, and the standard query parameters are
    declared once for the whole document. A shared class would have carried a ``kind`` field that
    picked a parser, which is a type doing a type's job in a string.
    """

    name: str
    spec_url: str
    mount: tuple[str, ...]
    strip: str = ""
    credentials: tuple[Credential, ...] = ()

    @property
    def endpoint(self) -> str:
        return self.spec_url

    def divergences(
        self, credentials: Mapping[str, str] | None = None, *, timeout: float = 120.0
    ) -> list[Finding]:
        return google_discovery_diff.divergences(self, timeout=timeout)


@dataclass(frozen=True)
class GraphQLComparison:
    """A source compared against the schema its vendor answers introspection with.

    Introspection IS the contract, which is what separates this from the published documents the
    published documents are compared against: a field absent from it is absent from the API, not
    undocumented. The cost is a credential, and that is why these run on a schedule rather than on
    a pull request.
    """

    name: str
    endpoint: str
    credentials: tuple[Credential, ...]
    # How the vendor wants the credential presented. Measured against each service, not assumed:
    # Fireflies takes `Bearer <key>`, and Linear's personal API keys go in BARE — sending Linear a
    # `Bearer` prefix is answered 400, not 401.
    auth_scheme: str = "Bearer"

    def authorization(self, resolved: Mapping[str, str]) -> str:
        key = resolved["api_key"]
        return f"{self.auth_scheme} {key}".strip() if self.auth_scheme else key

    def divergences(
        self, credentials: Mapping[str, str] | None = None, *, timeout: float = 120.0
    ) -> list[Finding]:
        return graphql_diff.divergences(self, credentials, timeout=min(timeout, 60.0))


@dataclass(frozen=True)
class ProbeComparison:
    """A source whose contract cannot be read off a document, so it is compared behaviourally.

    S3 dispatches on the query string rather than on the path, which no path diff can see. The
    model is still the vendor's own — botocore's service definition, the same file every AWS SDK
    is generated from — but what it is compared against is a running Backlot's answers.

    Note that AWS publishes a machine-readable model for S3 too, and this is still not a
    :class:`SpecComparison`. What separates the two is not whether a document exists but whether it
    enumerates operations as PATHS: everything the other class compares does, and S3 does not,
    because it selects operations by query string.
    """

    name: str
    spec_url: str
    # The source names its own prober rather than the dispatcher deriving one from the source's
    # name. A convention like `backlot.fidelity.{name}_probe` would turn a rename into an
    # ImportError on a nightly run instead of something a linter catches, and nothing static could
    # follow the link.
    run: Callable[[str, Mapping[str, Any], float], list[Finding]]

    @property
    def endpoint(self) -> str:
        return self.spec_url

    # None: a probe asks Backlot, not the vendor, and signs with the corpus's own keys. A probe
    # against a live vendor would declare what it needs here, and nothing else would change.
    credentials: tuple[Credential, ...] = ()

    def divergences(
        self, credentials: Mapping[str, str] | None = None, *, timeout: float = 120.0
    ) -> list[Finding]:
        return s3_probe.divergences(self, timeout=timeout)


OPENAPI = {
    "slack": OpenAPIComparison(
        name="slack",
        spec_url="https://raw.githubusercontent.com/slackapi/slack-api-specs/master/web-api/slack_web_openapi_v2.json",
        mount=("/slack/api",),
        strip="/slack/api",
    ),
    "github": OpenAPIComparison(
        name="github",
        spec_url="https://raw.githubusercontent.com/github/rest-api-description/main/descriptions/api.github.com/api.github.com.json",
        mount=("/github",),
        strip="/github",
    ),
    "jira": OpenAPIComparison(
        name="jira",
        spec_url="https://developer.atlassian.com/cloud/jira/platform/swagger-v3.v3.json",
        # v3 only. Backlot serves the `/rest/api/2` aliases too, because real Jira does, but
        # Atlassian publishes a v3 document — comparing a v2 path against it would report Backlot
        # inventing every one of them, which is a statement about the document, not about Jira.
        mount=("/atlassian/rest/api/3",),
        strip="/atlassian",
    ),
    "confluence": OpenAPIComparison(
        name="confluence",
        spec_url="https://developer.atlassian.com/cloud/confluence/swagger.v3.json",
        mount=("/atlassian/wiki",),
        strip="/atlassian",
    ),
    "notion": OpenAPIComparison(
        name="notion",
        spec_url="https://developers.notion.com/openapi.json",
        mount=("/notion/v1",),
        strip="/notion",
    ),
    "hubspot": OpenAPIComparison(
        name="hubspot",
        spec_url="https://api.hubspot.com/public/api/spec/v1/specs",
        # crm/v3 only: the v4 associations surface Backlot also serves is a SEPARATE API in
        # HubSpot's catalog with its own document, so comparing it against this one would report
        # it as invented.
        mount=("/hubspot/crm/v3",),
        strip="/hubspot",
        resolve_url=hubspot_catalog.entry("Custom Objects", "3"),
    ),
    # S3 is not an entry here: its contract is not a path map at all, so it is compared by asking
    # a running server instead. See backlot.fidelity.s3_probe.
}


GOOGLE_DISCOVERY = {
    "gmail": GoogleDiscoveryComparison(
        name="gmail",
        spec_url="https://gmail.googleapis.com/$discovery/rest?version=v1",
        # Nothing stripped: Google's own document already spells `gmail/v1/...`, which is exactly
        # where Backlot mounts it.
        mount=("/gmail",),
    ),
    "google_drive": GoogleDiscoveryComparison(
        name="google_drive",
        spec_url="https://www.googleapis.com/discovery/v1/apis/drive/v3/rest",
        mount=("/drive/v3",),
    ),
}


GRAPHQL = {
    "fireflies": GraphQLComparison(
        name="fireflies",
        endpoint="https://api.fireflies.ai/graphql",
        credentials=(Credential("api_key", "FIREFLIES_API_KEY", "a Fireflies API key"),),
    ),
    "linear": GraphQLComparison(
        name="linear",
        endpoint="https://api.linear.app/graphql",
        credentials=(Credential("api_key", "LINEAR_API_KEY", "a Linear personal API key"),),
        auth_scheme="",
    ),
}

PROBE = {
    "s3": ProbeComparison(
        name="s3",
        spec_url="https://raw.githubusercontent.com/boto/botocore/develop/botocore/data/s3/2006-03-01/service-2.json",
        run=s3_probe.run,
    ),
}

# The three kinds differ in what a vendor gives us to compare against, not in what they are for.
Comparison = OpenAPIComparison | GoogleDiscoveryComparison | GraphQLComparison | ProbeComparison

COMPARISONS: dict[str, Comparison] = {**OPENAPI, **GOOGLE_DISCOVERY, **GRAPHQL, **PROBE}


def _resolve_credentials(
    comparison: "Comparison", overrides: Mapping[str, str] | None = None
) -> dict[str, str]:
    """Every credential a source declares, from ``--credential`` first and the environment second.

    A name the source does not declare is an error rather than something quietly dropped: passing
    `--credential token=...` to a source that reads `api_key` used to do nothing at all, and the
    run then failed further along complaining about the environment instead.
    """
    declared = {c.name: c for c in comparison.credentials}
    unknown = sorted(set(overrides or ()) - set(declared))
    if unknown:
        wanted = ", ".join(sorted(declared)) or "none"
        raise FidelityError(
            f"{comparison.name} takes no credential called {unknown[0]!r} (it takes: {wanted})"
        )
    resolved, missing = {}, []
    for name, credential in declared.items():
        value = (overrides or {}).get(name) or os.environ.get(credential.env, "")
        if not value:
            missing.append(f"{name} ({credential.what}) — set {credential.env}")
        resolved[name] = value
    if missing:
        raise FidelityError(
            f"no credential for {comparison.name}: " + "; ".join(missing) + ". "
            "Pass --credential <name>=<value>, or put it in the environment."
        )
    return resolved


def divergences(
    comparison: "Comparison",
    credentials: Mapping[str, str] | None = None,
    *,
    timeout: float = 120.0,
) -> list[Finding]:
    """Every divergence for one source, whichever kind of contract its vendor publishes.

    Each kind knows how to load its own two sides and compare them, so this neither branches on the
    kind nor unpacks the source: it resolves whatever credentials the source declares, then asks.
    """
    if not isinstance(
        comparison,
        (OpenAPIComparison, GoogleDiscoveryComparison, GraphQLComparison, ProbeComparison),
    ):
        raise FidelityError(
            f"{type(comparison).__name__} is not a kind of comparison this knows how to run"
        )
    # Resolved here, for every kind, so a credential handed to a source that declares none is
    # refused rather than dropped on the floor — the kinds that need nothing are the majority, and
    # they are exactly where a silently ignored option goes unnoticed.
    return comparison.divergences(_resolve_credentials(comparison, credentials), timeout=timeout)


def baseline_path(source: str) -> Path:
    """Where a source's acknowledged divergences live.

    Inside the package, not beside the checkout: the baseline is the written record of what Backlot
    claims about a vendor, so it travels with the code that makes the claim and an installed copy
    can be compared against its vendor without the repository.
    """
    return Path(__file__).parent / "baseline" / f"{source}.json"
