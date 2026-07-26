"""Point official LlamaIndex readers at an enterprise-mock server.

Each `llama-index-readers-*` package normally targets a real SaaS host. Four accept a custom
host via constructor args (GitHub `base_url`, Jira `PATauth.server_url`, Confluence `base_url`,
S3 `s3_endpoint_url`); four hardcode it and need a small shim, all isolated here:

  - Slack: `slack_reader_at` builds the reader with the underlying slack_sdk `WebClient` already
    pointed at the mock. `SlackReader.__init__` doesn't just stash the client — it eagerly calls
    `client.api_test()` *during construction*, before the caller has a `reader._client` to set
    `base_url` on, so "construct, then set `_client.base_url`" alone isn't enough: that eager
    call would hit the real `https://slack.com/api/` default first. `slack_reader_at` swaps the
    `slack_sdk` module's `WebClient` for a subclass defaulting to the mock's base_url for the
    duration of construction (restored after), so even that first call lands on the mock (which
    now serves `api.test`, see `app/routers/slack.py`) — then sets `_client.base_url` again
    explicitly, since slack_sdk builds request URLs as `base_url + method`.
  - Gmail/Drive: `point_gmail_at` / `point_drive_at` wrap a `build` symbol to inject
    `client_options(api_endpoint=...)` + `static_discovery=True` (as the SDK examples do).
    `GmailReader.load_data()` does a *local* `from googleapiclient.discovery import build` on
    every call rather than importing it at module scope (confirmed empirically —
    `'build' in dir(llama_index.readers.google.gmail.base)` is `False`), so there is no
    `gm.build` module attribute to wrap; `point_gmail_at` wraps `googleapiclient.discovery.build`
    itself instead, one level up the chain (the local import re-reads whatever that symbol
    currently is at call time, so this has the same effect). Credential wiring differs between
    the two readers, though: `GoogleDriveReader` accepts `service_account_key=` directly, a real
    injection hook, so the admin path needs no monkeypatch beyond `point_drive_at`; `GmailReader`
    has no such hook — its `_get_credentials()` unconditionally runs a local disk-based OAuth
    flow — so `gmail.py` additionally patches `GmailReader._get_credentials` to hand back the
    mock-issued credential.
  - Notion: `patch_notion_at` rebinds the module-level URL constants.

This module also re-exports the serve/credential helpers from the sibling
`using-official-sdk/_mockserver.py`, so these scripts share the same `--url` / `--token` behavior
and local-fallback mock.
"""
from __future__ import annotations

import sys
from pathlib import Path

# Reuse the official-SDK examples' mock plumbing rather than duplicating it.
_SDK_DIR = Path(__file__).resolve().parent.parent / "using-official-sdk"
_inserted_sdk_dir = str(_SDK_DIR) not in sys.path
if _inserted_sdk_dir:
    sys.path.insert(0, str(_SDK_DIR))

from _mockserver import (  # noqa: E402
    google_oauth_user,
    google_service_account_info,
    serve_or_connect,
)

# `using-official-sdk/` holds same-named sibling scripts (jira.py, github.py, s3.py, ...). Leaving
# that dir on sys.path after the import above would let a later `from jira import JIRA` inside a
# llama-index reader's __init__ (e.g. JiraReader) resolve to `using-official-sdk/jira.py` instead
# of the real PyPI `jira` package. Nothing here needs the dir past the `_mockserver` import above,
# so drop it immediately rather than leaving a shadow trap for every subsequent import in the
# process (mirrors `drop_self_from_syspath`, but for a directory this module — not the caller —
# inserted).
if _inserted_sdk_dir:
    sys.path.remove(str(_SDK_DIR))

__all__ = [
    "serve_or_connect", "google_oauth_user", "google_service_account_info",
    "slack_base_url", "slack_reader_at", "notion_base_url", "s3_base_url", "github_base_url",
    "atlassian_base_url", "drop_self_from_syspath",
    "point_gmail_at", "point_drive_at", "patch_notion_at", "patch_s3fs_walk",
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
    """Work around a multi-year fsspec/s3fs compatibility bug (present since at least the
    2023.x releases and reproducing on every version installable today, including fsspec/s3fs
    2026.6.0): `S3Reader.load_data()` in whole-bucket mode (no `key`) calls
    `SimpleDirectoryReader._add_files`, which always does
    `fs.walk(input_dir, topdown=True, maxdepth=...)`. The sync `AbstractFileSystem.walk` declares
    `topdown` as an explicit parameter (so it never reaches `ls`), but `S3FileSystem` is async and
    its `_walk` chain bottoms out in `_ls()`, which doesn't accept `topdown`, raising
    ``TypeError: S3FileSystem._ls() got an unexpected keyword argument 'topdown'``. This is a
    client-side bug independent of the mock (reproduces identically against real AWS S3 with the
    same library versions), so no mock-side change can fix it.

    Wraps the *original* `S3FileSystem._walk` (captured before patching) rather than delegating
    to the shared `fsspec.asyn.AsyncFileSystem._walk` base implementation: `S3FileSystem` defines
    its own `_walk` with S3-specific logic (e.g. a guard raising `ValueError("Cannot crawl all of
    S3")` for bucket-less/root crawls) before calling up to the async base — bypassing it via
    `AsyncFileSystem._walk` directly would silently drop that guard (and any other S3-specific
    behavior) for every whole-bucket walk. Wrapping the original preserves all of it; the wrapper
    only strips the offending `topdown` kwarg. Scoped to `S3FileSystem` only, so other async
    fsspec backends (gcsfs, adlfs, ...) are unaffected. Self-verifies against `S3FileSystem._ls`'s
    signature first and no-ops if a future s3fs release has fixed the signature to accept
    `topdown` (directly or via a `**kwargs` catch-all), so we never silently drop a `topdown` a
    fixed s3fs would legitimately honor. Idempotent; safe to call more than once or from multiple
    scripts in the same process."""
    import inspect

    from s3fs.core import S3FileSystem

    if getattr(S3FileSystem._walk, "_mock_patched", False):
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

    _walk._mock_patched = True
    S3FileSystem._walk = _walk


def point_gmail_at(base_url: str) -> None:
    """Redirect GmailReader at the mock.

    GmailReader builds its Google service with googleapiclient's `build` and no host override.
    Its `load_data()` does a *local* `from googleapiclient.discovery import build` on every call
    rather than importing it at module scope, so there is no `gm.build` module attribute to wrap
    (confirmed empirically: `'build' in dir(llama_index.readers.google.gmail.base)` is `False`).
    Wrap `googleapiclient.discovery.build` itself instead — the local import re-reads whatever
    that symbol currently is at call time, so patching it one level up the chain has the same
    effect as patching `gm.build` would. Injects `client_options(api_endpoint=...)` +
    `static_discovery=True`, same as `using-official-sdk/gmail.py` (for Gmail the api_endpoint is
    the base itself, NOT `base + /gmail/v1` — the bundled discovery doc's rootUrl is replaced and
    the client appends `/gmail/v1`). Idempotent; fails loudly if the target `build` symbol is
    gone rather than silently letting the reader hit real googleapis.com.
    """
    from google.api_core.client_options import ClientOptions
    from googleapiclient import discovery

    base = base_url.rstrip("/")
    if not hasattr(discovery, "build"):
        raise RuntimeError("point_gmail_at: googleapiclient.discovery.build is gone — update the shim")
    if getattr(discovery.build, "_points_at_mock", False):
        return

    _real_build = discovery.build

    def _build(*args, **kwargs):
        kwargs.setdefault("static_discovery", True)
        kwargs["client_options"] = ClientOptions(api_endpoint=base)  # gmail: rootUrl replaced
        return _real_build(*args, **kwargs)

    _build._points_at_mock = True
    discovery.build = _build


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
    `using-official-sdk/gdrive.py` vs `gmail.py`). Idempotent; fails loudly if the target `build`
    symbol is gone rather than silently letting the reader hit real googleapis.com.
    """
    from google.api_core.client_options import ClientOptions
    from googleapiclient import discovery

    base = base_url.rstrip("/")
    if not hasattr(discovery, "build"):
        raise RuntimeError("point_drive_at: googleapiclient.discovery.build is gone — update the shim")
    if getattr(discovery.build, "_points_at_mock", False):
        return

    _real_build = discovery.build

    def _build(*args, **kwargs):
        kwargs.setdefault("static_discovery", True)
        kwargs["client_options"] = ClientOptions(api_endpoint=f"{base}/drive/v3")
        return _real_build(*args, **kwargs)

    _build._points_at_mock = True
    discovery.build = _build


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
        raise RuntimeError("patch_notion_at found no Notion URL constants to rebind — reader layout "
                           "changed; update the shim before it silently hits api.notion.com")


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
    real = getattr(hubspot, "_enterprise_mock_real_HubSpot", hubspot.HubSpot)
    hubspot._enterprise_mock_real_HubSpot = real   # idempotent across repeated calls

    def _at_mock(*a, **kw):
        kw.setdefault("host", base)
        client = real(*a, **kw)
        host = client.crm.companies.basic_api.api_client.configuration.host
        if base not in host:
            raise RuntimeError(
                f"the HubSpot SDK is configured for {host!r}, not the mock at {base!r} — the "
                f"`host` kwarg was ignored. Upgrade: pip install -U 'hubspot-api-client>=12'")
        return client

    hubspot.HubSpot = _at_mock


def drop_self_from_syspath(file: str) -> None:
    """Remove a script's own directory from sys.path so a file named `jira.py` / `github.py`
    doesn't shadow the third-party `jira` / `github` package it (transitively) imports."""
    here = Path(file).resolve().parent
    sys.path[:] = [p for p in sys.path if p and Path(p).resolve() != here]
