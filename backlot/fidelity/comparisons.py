"""Which comparison each source gets, and what it needs to run.

Four kinds, and they differ in what each side of the comparison is. For the two GraphQL sources,
Backlot's side is the SDL the server builds its engine from and the vendor's is a live introspection
response, which needs a credential. For the eight document sources, Backlot's side is the app's own
``app.openapi()`` and the vendor's is one or more documents it publishes, which needs none. For
S3, whose
operations are selected by query string rather than by path, both sides are answers from a running
server that ``backlot.serve()`` starts.

Nine of the eleven need no credential. All eleven run on a schedule and never on a pull request:
drift is this project's bug, but it is never the bug of whichever pull request happens to be open
when a vendor ships a change.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping

from backlot.fidelity import (
    google_discovery_diff,
    graphql_diff,
    hubspot_catalog,
    openapi_diff,
    s3_probe,
)
from backlot.fidelity.errors import CredentialsMissing, FidelityError
from backlot.fidelity.findings import BREAKING, Finding


def _one_report(per_spec: list[list[Finding]]) -> list[Finding]:
    """Several documents' findings as one source's report: de-duplicated by key, then ordered the
    way a single document's are.

    Two documents of one source can declare the SAME path. Atlassian's do: 11 paths sit outside
    ``/rest/api/{2,3}`` — ``/rest/atlassian-connect/1/*``, ``/rest/forge/1/*`` and one internal
    worklog route — so both Jira documents carry them and each reports as ``missing_operation``
    twice. That is a different case from the mirrored ``/rest/api/2/...`` and ``/rest/api/3/...``
    entries, which are distinct paths a client can call and both belong in the baseline. A
    :class:`~backlot.fidelity.findings.Baseline` is keyed on ``kind:path`` and cannot hold the
    second copy, so leaving it in makes the file claim more than it can load and prints a genuinely
    new finding on such a path twice.

    Ordered here rather than left concatenated because ``diff_operations`` sorts breaking-first-
    then-path per document, and that ordering does not survive joining two. The baseline is written
    in the order it is given, so without this the next real change lands buried in a reordering of
    the whole file.
    """
    seen: dict[str, Finding] = {}
    for findings in per_spec:
        for f in findings:
            seen.setdefault(f.key, f)
    return sorted(seen.values(), key=lambda f: (f.severity != BREAKING, f.path, f.kind))


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
class Spec:
    """One published document, and the served paths it speaks for.

    A source is not always one document. Atlassian publishes Jira's v2 and v3 REST APIs separately
    and Backlot serves both; a Drive file is also read through Docs, Sheets and Slides, each of
    which publishes its own discovery document.

    ``strip`` belongs HERE and not on the comparison, because how much of the mount a vendor's own
    document repeats differs per document: measured, Backlot's ``/sheets/v4/...`` pairs against the
    Sheets discovery document only with ``strip="/sheets"``, while Gmail's document already spells
    ``gmail/v1/...`` and strips nothing. One ``strip`` per source cannot describe both.
    """

    spec_url: str
    mount: tuple[str, ...]
    strip: str = ""
    # Set when ``spec_url`` addresses an INDEX rather than the document itself: given what was
    # fetched, it returns the URL to fetch instead. A vendor that publishes one document per
    # release needs this — see `hubspot_catalog` for why pinning the resolved URL is
    # not the simplification it appears to be.
    #
    # Typed as the resolver CLASS and not as a bare callable, because `endpoint` below renders it
    # into the baseline's identity: a lambda would render as `<function … at 0x…>` and write a
    # fresh memory address into the file on every run.
    resolve_url: hubspot_catalog.Entry | None = None

    @property
    def endpoint(self) -> str:
        """What this document is called, for the baseline's identity and the CLI's header.

        ``spec_url`` alone is not always the document. HubSpot's is an INDEX, and ``resolve_url``
        decides which of its entries to read — so two Specs over that index would otherwise report
        the same URL twice and name neither document."""
        if self.resolve_url is None:
            return self.spec_url
        return f"{self.spec_url}#{self.resolve_url}"


@dataclass(frozen=True)
class OpenAPIComparison:
    """A source compared against the OpenAPI document(s) its vendor publishes.

    Public, so this kind needs no credential, no quota and no account: the scheduled job carries no
    secret for it, and anyone can reproduce a finding locally.

    SEVERAL documents where a vendor publishes several: Atlassian publishes Jira's v2 and v3 REST
    APIs separately and Backlot serves both, and HubSpot's associations are their own API at their
    own version. Each is a :class:`Spec` with its own mount and strip, and the findings
    concatenate.
    """

    name: str
    specs: tuple[Spec, ...]
    credentials: tuple[Credential, ...] = ()

    @property
    def endpoints(self) -> tuple[str, ...]:
        return tuple(s.endpoint for s in self.specs)

    def divergences(
        self, credentials: Mapping[str, str] | None = None, *, timeout: float = 120.0
    ) -> list[Finding]:
        # One memo across this run's specs: HubSpot reaches every document through a single index,
        # so without it a source comparing two of its APIs reads that index once per spec.
        fetched: dict[str, dict] = {}
        return _one_report(
            [openapi_diff.divergences(s, timeout=timeout, seen=fetched) for s in self.specs]
        )


@dataclass(frozen=True)
class GoogleDiscoveryComparison:
    """A source compared against the Google API Discovery document its vendor publishes.

    Its own class rather than a flag on the one above, because Discovery is not OpenAPI: methods
    nest under resources, paths join to a ``servicePath``, and the standard query parameters are
    declared once for the whole document. A shared class would have carried a ``kind`` field that
    picked a parser, which is a type doing a type's job in a string.

    SEVERAL documents where a source is served through several APIs: a Drive file is also read
    through Docs, Sheets and Slides, each of which publishes its own discovery document.
    """

    name: str
    specs: tuple[Spec, ...]
    credentials: tuple[Credential, ...] = ()

    @property
    def endpoints(self) -> tuple[str, ...]:
        return tuple(s.endpoint for s in self.specs)

    def divergences(
        self, credentials: Mapping[str, str] | None = None, *, timeout: float = 120.0
    ) -> list[Finding]:
        return _one_report(
            [google_discovery_diff.divergences(s, timeout=timeout) for s in self.specs]
        )


@dataclass(frozen=True)
class GraphQLComparison:
    """A source compared against the schema its vendor answers introspection with.

    Introspection IS the contract, which is what separates this from a published document: a field
    absent from it is absent from the API, not merely undocumented. The cost is a credential, which
    is why these two are the sources that go uncompared when one is not set.
    """

    name: str
    endpoint: str
    credentials: tuple[Credential, ...]
    # How the vendor wants the credential presented. Measured against each service, not assumed:
    # Fireflies takes `Bearer <key>`, and Linear's personal API keys go in BARE — sending Linear a
    # `Bearer` prefix is answered 400, not 401.
    auth_scheme: str = "Bearer"

    @property
    def endpoints(self) -> tuple[str, ...]:
        """One, always: introspection is a single live schema, not a set of documents. Plural so
        every kind answers the same question and no caller needs to know which it is holding."""
        return (self.endpoint,)

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

    Note that AWS publishes a machine-readable model for S3 too, and this is still neither an
    :class:`OpenAPIComparison` nor a :class:`GoogleDiscoveryComparison`. What separates them is not
    whether a document exists but whether it enumerates operations as PATHS: everything those two
    compare does, and S3 does not, because it selects operations by query string.
    """

    name: str
    spec_url: str
    # The source names its own prober rather than the dispatcher deriving one from the source's
    # name. A convention like `backlot.fidelity.{name}_probe` would turn a rename into an
    # ImportError on a nightly run instead of something a linter catches, and nothing static could
    # follow the link.
    run: Callable[[str, Mapping[str, Any], float], list[Finding]]

    @property
    def endpoints(self) -> tuple[str, ...]:
        return (self.spec_url,)

    # Which served paths this comparison speaks for. Every kind answers that question; only a
    # probe was never asked it, because it needs no mount to select paths out of a document — it
    # asks a running server instead. The prober does not read this; what does is the check that
    # every served path is compared by something.
    mount: tuple[str, ...] = ()
    # None: a probe asks Backlot, not the vendor, and signs with the corpus's own keys. A probe
    # against a live vendor would declare what it needs here. Note that `run` is the only part a
    # probe supplies: fetching the model and starting the server are `s3_probe.divergences`, and
    # every probe goes through it.
    credentials: tuple[Credential, ...] = ()

    def divergences(
        self, credentials: Mapping[str, str] | None = None, *, timeout: float = 120.0
    ) -> list[Finding]:
        return s3_probe.divergences(self, timeout=timeout)


OPENAPI = {
    "slack": OpenAPIComparison(
        name="slack",
        specs=(
            Spec(
                spec_url="https://raw.githubusercontent.com/slackapi/slack-api-specs/master/web-api/slack_web_openapi_v2.json",
                mount=("/slack/api",),
                strip="/slack/api",
            ),
        ),
    ),
    "github": OpenAPIComparison(
        name="github",
        specs=(
            Spec(
                spec_url="https://raw.githubusercontent.com/github/rest-api-description/main/descriptions/api.github.com/api.github.com.json",
                mount=("/github",),
                strip="/github",
            ),
        ),
    ),
    "jira": OpenAPIComparison(
        name="jira",
        specs=(
            Spec(
                spec_url="https://developer.atlassian.com/cloud/jira/platform/swagger-v3.v3.json",
                mount=("/atlassian/rest/api/3",),
                strip="/atlassian",
            ),
            # Backlot serves the `/rest/api/2` paths because the clients call them: measured,
            # `atlassian-python-api` and the `jira` PyPI client both use v2 exclusively at their
            # default settings, the latter probing `/rest/api/2/serverInfo` on connect.
            #
            # Its OWN document, not the v3 one. Atlassian names these
            # `swagger[-<apiVersion>].<oasVersion>.json`, so the suffix-less file is the v2 API —
            # 403 `/rest/api/2` paths, none of them v3. Measured against the v3 document instead,
            # all six served v2 operations report as surface Backlot invented, which is a
            # statement about the document rather than about Jira.
            Spec(
                spec_url="https://developer.atlassian.com/cloud/jira/platform/swagger.v3.json",
                mount=("/atlassian/rest/api/2",),
                strip="/atlassian",
            ),
        ),
    ),
    "confluence": OpenAPIComparison(
        name="confluence",
        specs=(
            Spec(
                spec_url="https://developer.atlassian.com/cloud/confluence/swagger.v3.json",
                mount=("/atlassian/wiki",),
                strip="/atlassian",
            ),
        ),
    ),
    "notion": OpenAPIComparison(
        name="notion",
        specs=(
            Spec(
                spec_url="https://developers.notion.com/openapi.json",
                mount=("/notion/v1",),
                strip="/notion",
            ),
        ),
    ),
    "hubspot": OpenAPIComparison(
        name="hubspot",
        specs=(
            Spec(
                spec_url="https://api.hubspot.com/public/api/spec/v1/specs",
                mount=("/hubspot/crm/v3",),
                strip="/hubspot",
                resolve_url=hubspot_catalog.entry("Custom Objects", "3"),
            ),
            # Associations are their own API at their own version, published as its own document
            # in the same index. The CRM v3 document does not declare them, so comparing the
            # association read against it would report it as invented.
            Spec(
                spec_url="https://api.hubspot.com/public/api/spec/v1/specs",
                mount=("/hubspot/crm/v4",),
                strip="/hubspot",
                resolve_url=hubspot_catalog.entry("Associations", "4"),
            ),
        ),
    ),
    # S3 is not an entry here: its contract is not a path map at all, so it is compared by asking
    # a running server instead. See backlot.fidelity.s3_probe.
}


GOOGLE_DISCOVERY = {
    "gmail": GoogleDiscoveryComparison(
        name="gmail",
        specs=(
            Spec(
                spec_url="https://gmail.googleapis.com/$discovery/rest?version=v1",
                # Nothing stripped: Google's own document already spells `gmail/v1/...`, which is
                # exactly where Backlot mounts it.
                mount=("/gmail",),
            ),
        ),
    ),
    "google_drive": GoogleDiscoveryComparison(
        name="google_drive",
        specs=(
            Spec(
                spec_url="https://www.googleapis.com/discovery/v1/apis/drive/v3/rest",
                mount=("/drive/v3",),
            ),
            # A Drive file is also read through the editor APIs, and each publishes its own
            # discovery document. Each strips its own mount, unlike Drive: these three declare an
            # empty `servicePath` and spell the version themselves (`v4/spreadsheets/...`), where
            # Drive's document repeats `drive/v3/`. Measured — with nothing stripped, every served
            # operation reports as one Backlot invented.
            #
            # The version is in the mount, matching `gen_docs.SOURCES`. A bare `/docs` would also
            # select FastAPI's own `/docs` and `/docs/oauth2-redirect`, which escape this only
            # because `include_in_schema=False` keeps them out of `app.openapi()`.
            Spec(
                spec_url="https://www.googleapis.com/discovery/v1/apis/docs/v1/rest",
                mount=("/docs/v1",),
                strip="/docs",
            ),
            Spec(
                spec_url="https://www.googleapis.com/discovery/v1/apis/sheets/v4/rest",
                mount=("/sheets/v4",),
                strip="/sheets",
            ),
            Spec(
                spec_url="https://www.googleapis.com/discovery/v1/apis/slides/v1/rest",
                mount=("/slides/v1",),
                strip="/slides",
            ),
        ),
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
        mount=("/s3",),
        run=s3_probe.run,
    ),
}


# Served paths no published document covers, and why. Matched by prefix, so `/batch` covers
# `/batch/{api}/{version}`.
#
# The escape hatch the coverage check is built around, and deliberately narrow: an entry here is a
# reason a reviewer reads, not a silence. Two kinds qualify — Backlot's own surface, which no
# vendor publishes because it is not a vendor's, and a vendor surface that genuinely has no
# document to compare against.
UNCOMPARED = {
    "/health": "Backlot's own liveness endpoint, not a vendor's surface",
    "/oauth2/token": (
        "Backlot's own token exchange. The vendors' own auth surfaces are compared under their "
        "mounts; this one is how a caller gets a token for Backlot itself."
    ),
    "/_meta": (
        "Backlot's own introspection — the corpus's principals and the per-source OpenAPI. No "
        "vendor has it, by construction."
    ),
    "/batch": (
        "Google's batch protocol is one endpoint carrying requests for every Google API, so no "
        "single discovery document declares it: Drive's, Gmail's and the editor APIs' each "
        "describe only their own operations."
    ),
}

# The four kinds differ in what a vendor gives us to compare against, not in what they are for.
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
        raise CredentialsMissing(
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
