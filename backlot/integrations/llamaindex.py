"""Point official LlamaIndex readers at a Backlot server.

Each `llama-index-readers-*` package normally targets a real SaaS host. Four take a custom host
via constructor args (GitHub `base_url`, Jira `PATauth.server_url`, Confluence `base_url`, S3
`s3_endpoint_url`); four hardcode it and need a shim, all isolated here. Each shim's docstring
says what seam it uses and why that one:

  - Slack: `slack_reader_at` — the reader calls `api_test()` DURING construction, so the client
    has to arrive already pointed at the mock.
  - Gmail/Drive: `point_gmail_at` / `point_drive_at` — wrap `build` to inject
    `client_options(api_endpoint=...)`.
  - Notion: `patch_notion_at` — rebind the module-level URL constants.
  - Linear: `patch_linear_at` — swap the module's `requests` for a URL-rewriting proxy.
"""

from __future__ import annotations

__all__ = [
    "slack_base_url",
    "slack_reader_at",
    "notion_base_url",
    "s3_base_url",
    "github_base_url",
    "atlassian_base_url",
    "linear_base_url",
    "point_gmail_at",
    "point_drive_at",
    "patch_notion_at",
    "patch_s3fs_walk",
    "point_hubspot_at",
    "patch_linear_at",
]


def slack_base_url(base_url: str) -> str:
    """Slack Web API base for `reader._client.base_url` — trailing slash required (slack_sdk
    builds request URLs as `base_url + method`, e.g. `conversations.history`)."""
    return f"{base_url.rstrip('/')}/slack/api/"


def slack_reader_at(base_url: str, token: str):
    """Build a `SlackReader` with its `WebClient` pointed at the mock from the very first call.

    `SlackReader.__init__` eagerly calls `client.api_test()` before returning, using whatever
    `base_url` the client was constructed with (there's no constructor arg to pass one in). Left
    alone that call goes to the real `https://slack.com/api/` default. `SlackReader.__init__`
    does a *local* `from slack_sdk import WebClient` on every call, so temporarily swapping the
    `slack_sdk` module's `WebClient` attribute for a subclass that defaults `base_url` to the
    mock — for the duration of this one construction only, restored in `finally` — redirects
    that eager call to the mock instead. `reader._client.base_url` is set again explicitly
    afterward for clarity, though the patched default already applied it.
    """
    import slack_sdk
    from llama_index.readers.slack import SlackReader

    mocked_url = slack_base_url(base_url)
    real_web_client = slack_sdk.WebClient

    class _WebClientAtMock(real_web_client):
        def __init__(self, *args, **kwargs):
            kwargs.setdefault("base_url", mocked_url)
            super().__init__(*args, **kwargs)

    slack_sdk.WebClient = _WebClientAtMock
    try:
        reader = SlackReader(slack_token=token)
    finally:
        slack_sdk.WebClient = real_web_client
    reader._client.base_url = mocked_url
    return reader


def notion_base_url(base_url: str) -> str:
    """Notion base for `patch_notion_at` — the reader appends the `/v1/...` path itself."""
    return f"{base_url.rstrip('/')}/notion"


def s3_base_url(base_url: str) -> str:
    """S3 endpoint for `S3Reader(s3_endpoint_url=...)` (path-style under `/s3`)."""
    return f"{base_url.rstrip('/')}/s3"


def github_base_url(base_url: str) -> str:
    """GitHub REST base for `GitHubIssuesClient(base_url=...)`."""
    return f"{base_url.rstrip('/')}/github"


def atlassian_base_url(base_url: str) -> str:
    """Atlassian base for Jira `PATauth.server_url` / `ConfluenceReader(base_url=...)`. The
    Jira client appends `/rest/api/<ver>` itself. atlassian-python-api 4.0.7 never appends
    `/wiki` regardless of `cloud`, so the Confluence example spells `/wiki` out explicitly on
    top of this base (with `cloud=False`) rather than relying on the client to add it."""
    return f"{base_url.rstrip('/')}/atlassian"


def patch_s3fs_walk() -> None:
    """Work around a long-standing fsspec/s3fs bug, NOT anything mock-side (it reproduces
    identically against real AWS S3): a whole-bucket `S3Reader.load_data()` reaches
    `fs.walk(..., topdown=True)`, and `S3FileSystem` is async so its `_walk` chain bottoms out in
    `_ls()`, which does not accept `topdown`.

    Wraps the ORIGINAL `S3FileSystem._walk` rather than delegating to
    `AsyncFileSystem._walk`: S3's own `_walk` carries S3-specific logic (a guard against crawling
    all of S3) that going straight to the base class would silently drop. The wrapper only strips
    the offending kwarg. Scoped to `S3FileSystem`, idempotent, and self-verifying — it no-ops if a
    future s3fs accepts `topdown`, so a fixed library's kwarg is never dropped."""
    import inspect

    from s3fs.core import S3FileSystem

    if getattr(S3FileSystem._walk, "_backlot_patched", False):
        return

    ls_params = inspect.signature(S3FileSystem._ls).parameters
    if "topdown" in ls_params or any(
        p.kind is inspect.Parameter.VAR_KEYWORD for p in ls_params.values()
    ):
        return  # upstream fixed; the topdown-stripping shim is no longer needed

    _original_walk = S3FileSystem._walk  # own definition if present, else inherited

    async def _walk(self, path, *args, **kwargs):
        kwargs.pop("topdown", None)
        async for item in _original_walk(self, path, *args, **kwargs):
            yield item

    _walk._backlot_patched = True
    S3FileSystem._walk = _walk


# googleapiclient serviceName ("gmail" / "drive") -> the api_endpoint it should be built with.
# Consulted, at call time, by the ONE shared wrapper `_ensure_google_build_wrapped` installs.
#
# point_gmail_at and point_drive_at used to each wrap `discovery.build` independently, guarded by
# the same `_points_at_mock` flag on the symbol. That flag records only THAT a wrapper is
# installed, not which function installed it or which endpoint it points at — so whichever ran
# second found the flag already set, returned immediately, and left its service silently pointed
# at the OTHER function's endpoint (Drive traffic hitting Gmail's `api_endpoint`, or vice versa).
# A per-service registry keyed by the `serviceName` `build()` is actually invoked with — llama-index
# calls `build("gmail", "v1", ...)` / `build("drive", "v3", ...)`, confirmed by reading both
# readers' source — lets both stay active at once, in either order, through one wrapper.
_MOCK_SERVICE_ENDPOINTS: dict[str, str] = {}


def _ensure_google_build_wrapped() -> None:
    """Install the shared `googleapiclient.discovery.build` wrapper, once. Safe to call from both
    `point_gmail_at` and `point_drive_at`, in either order, any number of times — it only wraps
    on the first call (checked via `_backlot_wrapped` on the symbol) and every call after that is
    a no-op here; the actual redirection happens through `_MOCK_SERVICE_ENDPOINTS`, updated by the
    caller after this returns.

    Clears `_MOCK_SERVICE_ENDPOINTS` whenever it (re)installs — i.e. exactly when `discovery.build`
    does NOT already carry the wrapper. That happens not just on the very first call, but also
    whenever something has reset `discovery.build` back to a plain callable since the wrapper was
    last installed — the tests in this repo do exactly that in a `finally:` block to undo a patch.
    Without the clear, entries set by a wrapper that's since been discarded would outlive it: e.g.
    `point_gmail_at(A)` installs the wrapper and registers "gmail"; something resets
    `discovery.build` directly; `point_drive_at(B)` alone reinstalls the wrapper (a fresh symbol,
    same dict) and registers "drive" — leaving a stale "gmail" -> A in the dict even though gmail
    was never touched in this round, and the reinstalled wrapper would silently honour it. The dict
    is process-global; the wrapper closure was not, before this function existed — reinstalling it
    is the only observable signal that the previous installation is gone, so that's the signal used.
    """
    from google.api_core.client_options import ClientOptions
    from googleapiclient import discovery

    if getattr(discovery.build, "_backlot_wrapped", False):
        return

    _MOCK_SERVICE_ENDPOINTS.clear()
    _real_build = discovery.build

    def _build(*args, **kwargs):
        service_name = args[0] if args else kwargs.get("serviceName")
        endpoint = _MOCK_SERVICE_ENDPOINTS.get(service_name)
        if endpoint is not None:
            kwargs.setdefault("static_discovery", True)
            kwargs["client_options"] = ClientOptions(api_endpoint=endpoint)
        return _real_build(*args, **kwargs)

    _build._backlot_wrapped = True
    discovery.build = _build


def point_gmail_at(base_url: str) -> None:
    """Redirect GmailReader at the mock.

    GmailReader builds its Google service with googleapiclient's `build` and no host override.
    Its `load_data()` does a *local* `from googleapiclient.discovery import build` on every call
    rather than importing it at module scope, so there is no `gm.build` module attribute to wrap
    (confirmed empirically: `'build' in dir(llama_index.readers.google.gmail.base)` is `False`).
    Wrap `googleapiclient.discovery.build` itself instead — the local import re-reads whatever
    that symbol currently is at call time, so patching it one level up the chain has the same
    effect as patching `gm.build` would. Injects `client_options(api_endpoint=...)` +
    `static_discovery=True`, same as `examples/using-official-sdk/gmail.py` (for Gmail the api_endpoint is
    the base itself, NOT `base + /gmail/v1` — the bundled discovery doc's rootUrl is replaced and
    the client appends `/gmail/v1`).

    The wrap point is SHARED with `point_drive_at` (see `_ensure_google_build_wrapped`) — both can
    be active at once, in either order, and each redirects only its own service. Idempotent for
    repeated calls with the same URL; fails loudly if the target `build` symbol is gone rather
    than silently letting the reader hit real googleapis.com.
    """
    from googleapiclient import discovery

    if not hasattr(discovery, "build"):
        raise RuntimeError(
            "point_gmail_at: googleapiclient.discovery.build is gone — update the shim"
        )
    _ensure_google_build_wrapped()
    _MOCK_SERVICE_ENDPOINTS["gmail"] = base_url.rstrip("/")  # gmail: rootUrl replaced as-is


def point_drive_at(base_url: str) -> None:
    """Redirect GoogleDriveReader at the mock.

    Same wrap point as `point_gmail_at`: `GoogleDriveReader` builds its Drive service with
    googleapiclient's `build` and no host override, and every method that needs it
    (`_get_fileids_meta`, `_download_file`) does a *local* `from googleapiclient.discovery import
    build` rather than importing it at module scope (confirmed empirically:
    `'build' in dir(llama_index.readers.google.drive.base)` is `False`), so there is no module
    attribute on `drive.base` to wrap. Wrap `googleapiclient.discovery.build` itself, one level up
    the chain, exactly as `point_gmail_at` does — the local imports re-read whatever that symbol
    currently is at call time. KEY DIFFERENCE from Gmail: Drive's bundled discovery doc's rootUrl
    already carries the `/drive/v3` service path, so the replacement `api_endpoint` must include
    it (`base + "/drive/v3"`); Gmail's api_endpoint is the base with no suffix (see
    `examples/using-official-sdk/gdrive.py` vs `gmail.py`).

    The wrap point is SHARED with `point_gmail_at` (see `_ensure_google_build_wrapped`) — both can
    be active at once, in either order, and each redirects only its own service. Idempotent for
    repeated calls with the same URL; fails loudly if the target `build` symbol is gone rather
    than silently letting the reader hit real googleapis.com.
    """
    from googleapiclient import discovery

    if not hasattr(discovery, "build"):
        raise RuntimeError(
            "point_drive_at: googleapiclient.discovery.build is gone — update the shim"
        )
    _ensure_google_build_wrapped()
    _MOCK_SERVICE_ENDPOINTS["drive"] = f"{base_url.rstrip('/')}/drive/v3"


def patch_notion_at(base_url: str) -> None:
    """Redirect NotionPageReader at the mock. The reader hardcodes the Notion host in module-level
    URL constants (no base_url arg); rebind every one that points at api.notion.com. Fails loudly
    if the expected constants are gone (a reader upgrade), rather than hitting the real host."""
    import llama_index.readers.notion.base as nb

    base = base_url.rstrip("/")
    overrides = {
        "BLOCK_CHILD_URL_TMPL": base + "/v1/blocks/{block_id}/children",
        "DATABASE_URL_TMPL": base + "/v1/databases/{database_id}/query",
        "SEARCH_URL": base + "/v1/search",
    }
    patched = 0
    for name, value in overrides.items():
        if hasattr(nb, name):
            setattr(nb, name, value)
            patched += 1
    # Catch any other hardcoded api.notion.com occurrence (e.g. single-page retrieval) the version
    # may add, so nothing silently escapes to the real host.
    for name in dir(nb):
        val = getattr(nb, name)
        if isinstance(val, str) and "api.notion.com" in val:
            setattr(nb, name, val.replace("https://api.notion.com", base))
            patched += 1
    if patched == 0:
        raise RuntimeError(
            "patch_notion_at found no Notion URL constants to rebind — reader layout "
            "changed; update the shim before it silently hits api.notion.com"
        )


def point_hubspot_at(base_url: str) -> None:
    """Redirect HubspotReader at the mock. The reader takes only an access token and builds
    ``HubSpot(access_token=...)`` itself — but it does ``from hubspot import HubSpot`` *inside*
    ``load_data()``, so rebinding the module attribute is enough and the reader needs no changes.

    ``host`` is a plain kwarg on the current SDK (``_default_api_factory`` copies unknown kwargs
    onto its Configuration). On 8.x it is silently IGNORED and the client talks to api.hubapi.com,
    so this asserts the override actually took rather than letting a "mock" run hit production.
    """
    import hubspot

    base = base_url.rstrip("/")
    real = getattr(hubspot, "_backlot_real_HubSpot", hubspot.HubSpot)
    hubspot._backlot_real_HubSpot = real  # idempotent across repeated calls

    def _at_mock(*a, **kw):
        kw.setdefault("host", base)
        client = real(*a, **kw)
        host = client.crm.companies.basic_api.api_client.configuration.host
        # Compare against the host actually requested, not the one captured when this was installed,
        # so the guard stays correct for a caller that passes its own `host`.
        if kw["host"] not in host:
            raise RuntimeError(
                f"the HubSpot SDK is configured for {host!r}, not {kw['host']!r} — the `host` kwarg "
                f"was ignored. Upgrade: pip install -U 'hubspot-api-client>=12'"
            )
        return client

    hubspot.HubSpot = _at_mock


def patch_linear_at(base_url: str) -> None:
    """Redirect LinearReader at the mock.

    Harder than the other shims, and the reason is worth stating: `LinearReader.load_data` sets
    ``graphql_endpoint = "https://api.linear.app/graphql"`` as a **local variable inside the
    method**. There is no constructor argument and no module-level constant, so the
    `patch_notion_at` trick — rebind a module attribute — has nothing to rebind.

    What IS patchable is the module's `requests` import: the reader does ``import requests`` at
    module scope and then calls ``requests.post(graphql_endpoint, ...)``. Swapping that one
    attribute for a thin proxy lets the URL be rewritten on the way out, leaving the reader
    untouched. Only api.linear.app is redirected, so a proxy left installed can't silently
    capture some other host's traffic.

    Idempotent; fails loudly if the reader stops importing `requests` (a rewrite that would
    otherwise send a "mock" run to the real api.linear.app).
    """
    import llama_index.readers.linear.base as lb

    base = base_url.rstrip("/")
    real = getattr(lb, "_backlot_real_requests", None) or getattr(lb, "requests", None)
    if real is None:
        raise RuntimeError(
            "patch_linear_at: llama_index.readers.linear.base no longer imports "
            "`requests` — update the shim before it hits api.linear.app"
        )
    lb._backlot_real_requests = real  # idempotent across repeated calls

    class _RequestsAtMock:
        """Forwards everything to the real `requests`, rewriting only Linear's hardcoded URL."""

        def __getattr__(self, name):
            return getattr(real, name)

        def post(self, url, *args, **kwargs):
            if url.startswith("https://api.linear.app"):
                url = url.replace("https://api.linear.app", base)
            return real.post(url, *args, **kwargs)

    lb.requests = _RequestsAtMock()


def linear_base_url(base_url: str) -> str:
    """Linear GraphQL base for `patch_linear_at` — the reader appends `/graphql` itself."""
    return f"{base_url.rstrip('/')}/linear"
