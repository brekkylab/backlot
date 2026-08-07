"""Read-only coverage: drive each official LlamaIndex reader against the mock.

Uses the `live_server` fixture (a real uvicorn on the conftest SAMPLE corpus) — readers make real
HTTP calls, so they need a listening port. One test per source; each self-skips if its reader
package is absent (installed via the `[llamaindex]` extra). Does not import from `examples/`
(repo rule) — the small point-at-the-mock setup is duplicated here.
"""

from __future__ import annotations

import pytest


def _base_token(live_server):
    base, settings = live_server
    return base, settings.admin_token


def test_github(live_server):
    pytest.importorskip("llama_index.readers.github")
    from llama_index.readers.github import GitHubRepositoryIssuesReader, GitHubIssuesClient

    base, admin = _base_token(live_server)
    client = GitHubIssuesClient(github_token=admin, base_url=f"{base}/github", verbose=False)
    reader = GitHubRepositoryIssuesReader(client, owner="acme", repo="gateway", verbose=False)
    docs = reader.load_data(state=GitHubRepositoryIssuesReader.IssueState.OPEN)
    assert docs, "expected at least one issue Document"
    assert any("refill is off by one tick" in d.text for d in docs)  # SAMPLE gh-issue-1 body (open)
    assert all("Corrects the refill tick" not in d.text for d in docs)  # gh-pr-1 (closed) excluded


def test_confluence(live_server):
    pytest.importorskip("llama_index.readers.confluence")
    from llama_index.readers.confluence import ConfluenceReader

    base, admin = _base_token(live_server)
    # atlassian-python-api 4.0.7 does not append `/wiki` itself regardless of `cloud`, so the
    # mock's `/atlassian/wiki/rest/api` root must be spelled out here (`cloud` only toggles
    # cloud-specific API shapes elsewhere, not the URL). `max_num_results` must be passed
    # explicitly: llama-index-readers-confluence 0.7.0's `load_data` forwards a bare `limit=None`
    # to `Confluence.get_all_pages_from_space`, which does `len(results) <= limit` and raises
    # `TypeError` when `limit` is None — a client-side bug independent of the mock/server.
    reader = ConfluenceReader(base_url=f"{base}/atlassian/wiki", cloud=False, api_token=admin)
    docs = reader.load_data(space_key="handbook", max_num_results=50)
    assert docs, "expected at least one page Document"
    assert any("How we build software" in d.text for d in docs)  # SAMPLE cf-handbook body
    assert all("Compensation Bands" not in d.text for d in docs)  # cf-comp (people-ops) excluded


def test_jira(live_server):
    pytest.importorskip("llama_index.readers.jira")
    from llama_index.readers.jira import JiraReader

    base, admin = _base_token(live_server)
    reader = JiraReader(PATauth={"server_url": f"{base}/atlassian", "api_token": admin})
    docs = reader.load_data(query="project = payments")
    assert docs, "expected at least one issue Document"
    assert any("checkout latency" in d.text for d in docs)  # SAMPLE jira-sev2 body

    # container scoping: an unresolvable project must yield zero issues, not the unfiltered
    # corpus (the same silent-no-op fidelity gap found & fixed for Confluence's space= handling
    # applied identically to Jira's project= handling -- see backlot/routers/atlassian.py).
    empty = reader.load_data(query="project = NOPE_DOES_NOT_EXIST")
    assert empty == []


def _slack_reader_at(base: str, token: str):
    """Build a `SlackReader` pointed at the mock.

    `SlackReader.__init__` doesn't just stash the slack_sdk `WebClient` — it eagerly calls
    `client.api_test()` *during construction*, before the caller gets any object back to set
    `_client.base_url` on. Left alone, that call goes to the real `https://slack.com/api/`
    default, so "set base_url after construction" (as the interface note says, and as slack_sdk's
    own `WebClient(base_url=...)` constructor arg would suggest) can't work here: construction
    itself fails/network-egresses first. `SlackReader.__init__` does a *local*
    `from slack_sdk import WebClient` on every call, so temporarily swapping the `slack_sdk`
    module's `WebClient` attribute for a subclass that defaults to the mock's base_url — for the
    duration of construction only — redirects that eager call to the mock instead (which now
    serves `api.test`, see `backlot/routers/slack.py`). Restored in `finally` so nothing leaks past
    this one construction. Duplicated from `examples/using-llamaindex-readers/slack.py` (tests
    don't import from examples).
    """
    import slack_sdk
    from llama_index.readers.slack import SlackReader

    mocked_url = f"{base.rstrip('/')}/slack/api/"  # trailing slash required (base_url + method)
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
    reader._client.base_url = (
        mocked_url  # already set via the patched default; explicit for clarity
    )
    return reader


def test_slack(live_server):
    pytest.importorskip("llama_index.readers.slack")

    base, admin = _base_token(live_server)
    reader = _slack_reader_at(base, admin)
    channels = reader._client.conversations_list(limit=200)["channels"]
    ids = [c["id"] for c in channels]
    docs = reader.load_data(channel_ids=ids)
    assert docs, "expected at least one channel Document"
    assert any("502" in d.text for d in docs)  # SAMPLE incidents channel


def _patch_s3fs_walk() -> None:
    """Work around a long-standing fsspec/s3fs bug, NOT anything mock-side (it reproduces
    identically against real AWS S3): a whole-bucket `S3Reader.load_data()` reaches
    `fs.walk(..., topdown=True)`, and `S3FileSystem` is async so its `_walk` chain bottoms out in
    `_ls()`, which does not accept `topdown`.

    Wraps the ORIGINAL `S3FileSystem._walk` rather than delegating to `AsyncFileSystem._walk`:
    S3's own `_walk` carries S3-specific logic (a guard against crawling all of S3) that going
    straight to the base class would silently drop. The wrapper only strips the offending kwarg.
    Scoped to `S3FileSystem`, idempotent, and self-verifying — it no-ops if a future s3fs accepts
    `topdown`, so a fixed library's kwarg is never dropped.

    Duplicated from `examples/using-llamaindex-readers/_llamaindex.py:patch_s3fs_walk` (tests
    don't import from examples)."""
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


def test_s3(live_server):
    pytest.importorskip("llama_index.readers.s3")
    pytest.importorskip("s3fs")
    from llama_index.readers.s3 import S3Reader
    from backlot import synth

    _patch_s3fs_walk()
    base, admin = _base_token(live_server)
    reader = S3Reader(
        bucket="eng-artifacts",
        s3_endpoint_url=f"{base}/s3",
        aws_access_id=synth.s3_access_key_id(admin),
        aws_access_secret=synth.s3_secret_access_key(admin),
        region_name="us-east-1",
    )
    docs = reader.load_data()
    assert docs, "expected at least one object Document"
    assert any("dashboards" in d.text for d in docs)  # SAMPLE s3-runbook body


def _patch_notion_at(base_url: str) -> None:
    """Redirect NotionPageReader at the mock. The reader hardcodes the Notion host in module-level
    URL constants (no base_url arg); rebind every one that points at api.notion.com. Fails loudly
    if the expected constants are gone (a reader upgrade), rather than hitting the real host.

    Duplicated from `examples/using-llamaindex-readers/_llamaindex.py:patch_notion_at` (tests
    don't import from examples)."""
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


def test_notion(live_server):
    pytest.importorskip("llama_index.readers.notion")
    from llama_index.readers.notion import NotionPageReader
    from backlot import synth

    base, admin = _base_token(live_server)
    _patch_notion_at(f"{base}/notion")
    reader = NotionPageReader(integration_token=admin)
    docs = reader.load_data(page_ids=[synth.notion_id("nt-runbook")])
    assert docs, "expected the runbook page as a Document"
    assert any("Check dashboards" in d.text for d in docs)  # SAMPLE nt-runbook body, case-correct


def _point_hubspot_at(monkeypatch, base_url: str) -> None:
    """Redirect HubspotReader at the mock.

    The reader builds ``HubSpot(access_token=...)`` itself but imports it INSIDE ``load_data()``, so
    rebinding the module attribute is enough. ``host`` is a plain kwarg on the current SDK and is
    silently IGNORED on 8.x — hence the loud failure, rather than a test that quietly hits
    api.hubapi.com.

    Through ``monkeypatch`` so it is undone at teardown: this rebinds a GLOBAL, and ``live_server``
    is module-scoped, so a leaked wrapper would outlive the port it was built for.
    """
    import hubspot

    base = base_url.rstrip("/")
    real = hubspot.HubSpot

    def _at_mock(*a, **kw):
        kw.setdefault("host", base)
        client = real(*a, **kw)
        host = client.crm.companies.basic_api.api_client.configuration.host
        assert kw["host"] in host, (
            f"SDK configured for {host!r}, not {kw['host']!r} — the `host` kwarg was ignored"
        )
        return client

    monkeypatch.setattr(hubspot, "HubSpot", _at_mock)


def test_hubspot(live_server, monkeypatch):
    pytest.importorskip("llama_index.readers.hubspot")
    from llama_index.readers.hubspot import HubspotReader

    base, admin = _base_token(live_server)
    _point_hubspot_at(monkeypatch, f"{base}/hubspot")
    # The reader returns one Document per object type (deals/contacts/companies), each holding the
    # str() of a list of SDK objects — its own design, not one Document per record.
    docs = {d.metadata.get("type"): d for d in HubspotReader(access_token=admin).load_data()}
    assert set(docs) == {"deals", "contacts", "companies"}
    assert "Acme Health" in docs["companies"].text  # SAMPLE hs-co-acme
    assert "ava@acme-health.com" in docs["contacts"].text  # SAMPLE hs-c-ava


def _point_gmail_at(base_url: str) -> None:
    """Redirect GmailReader at the mock.

    GmailReader builds its service with googleapiclient's `build` and no host override, and its
    `load_data()` imports `build` LOCALLY on every call — so there is no module attribute to wrap.
    Wrap `googleapiclient.discovery.build` itself instead, one level up: the local import re-reads
    that symbol at call time. Injects `client_options(api_endpoint=...)` + `static_discovery=True`,
    where the api_endpoint is the base ITSELF, not `base + /gmail/v1` — the bundled discovery doc's
    rootUrl is replaced and the client appends the version. Fails loudly if the symbol is gone
    rather than letting the reader reach real googleapis.com.

    Duplicated from `examples/using-llamaindex-readers/_llamaindex.py:point_gmail_at` (tests
    don't import from examples)."""
    from google.api_core.client_options import ClientOptions
    from googleapiclient import discovery

    base = base_url.rstrip("/")
    if not hasattr(discovery, "build"):
        raise RuntimeError(
            "point_gmail_at: googleapiclient.discovery.build is gone — update the shim"
        )
    if getattr(discovery.build, "_points_at_mock", False):
        return

    _real_build = discovery.build

    def _build(*args, **kwargs):
        kwargs.setdefault("static_discovery", True)
        kwargs["client_options"] = ClientOptions(api_endpoint=base)  # gmail: rootUrl replaced
        return _real_build(*args, **kwargs)

    _build._points_at_mock = True
    discovery.build = _build


def test_gmail(live_server):
    pytest.importorskip("llama_index.readers.google")
    from google.oauth2.credentials import Credentials
    from googleapiclient import discovery
    from llama_index.readers.google import GmailReader

    base, admin = _base_token(live_server)
    import llama_index.readers.google.gmail.base as gm

    # Both patches below are process-global (module attribute / class method), so restore them
    # after the test — left in place they'd leak into later tests in the same session, e.g.
    # test_sdk.py's `_gmail_svc` calling the real `googleapiclient.discovery.build` directly with
    # its own `client_options` (this shim's wrapper would clobber that with a stale base_url from
    # this test's already-shut-down live_server).
    _orig_build = discovery.build
    _orig_get_credentials = gm.GmailReader._get_credentials
    try:
        _point_gmail_at(base)

        # The installed GmailReader._get_credentials() unconditionally runs a local disk-based
        # OAuth flow (reads token.json / credentials.json off disk) every call, regardless of
        # whether a `service` (or any other credential) was already supplied — there's no
        # constructor hook to inject credentials directly (setting `reader.credentials` raises
        # `ValueError`: it isn't a declared pydantic field on this reader version). Patch the
        # method to hand back the admin bearer credential instead of touching disk;
        # `load_data()` then builds its own service via the wrapped `build` (since `service` is
        # left falsy), landing on the mock.
        gm.GmailReader._get_credentials = lambda self: Credentials(token=admin)

        reader = GmailReader(
            query="", service=None, use_iterative_parser=True, max_results=10, results_per_page=None
        )
        docs = reader.load_data()
        assert docs, "expected at least one Gmail Document"
        assert any("board" in d.text.lower() for d in docs)  # SAMPLE ceo mailbox
    finally:
        discovery.build = _orig_build
        gm.GmailReader._get_credentials = _orig_get_credentials


def _google_service_account_info(base_url: str) -> dict:
    """Fetch the mock's service-account key from `/_mock/credentials` (the mock-specific glue
    standing in for the JSON downloaded from the Cloud Console) — bare (no `subject`), so the
    resulting credential authenticates as the service account itself (admin, sees everything).

    Duplicated from `examples/using-official-sdk/_mockserver.py:google_service_account_info`
    (tests don't import from examples)."""
    import json
    import urllib.request

    with urllib.request.urlopen(f"{base_url.rstrip('/')}/_mock/credentials") as r:
        return json.load(r)["service_account"]


def _point_drive_at(base_url: str) -> None:
    """Redirect GoogleDriveReader at the mock.

    Same wrap point as `_point_gmail_at`: `GoogleDriveReader` builds its Drive service with
    googleapiclient's `build` and no host override, and every method that needs it
    (`_get_fileids_meta`, `_download_file`) does a *local* `from googleapiclient.discovery import
    build` rather than importing it at module scope (confirmed empirically:
    `'build' in dir(llama_index.readers.google.drive.base)` is `False`), so there is no module
    attribute on `drive.base` to wrap. Wrap `googleapiclient.discovery.build` itself, one level up
    the chain, exactly as `_point_gmail_at` does. KEY DIFFERENCE from Gmail: Drive's bundled
    discovery doc's rootUrl already carries the `/drive/v3` service path, so the replacement
    `api_endpoint` must include it (`base + "/drive/v3"`); Gmail's api_endpoint is the base with no
    suffix. Idempotent; fails loudly if the target `build` symbol is gone rather than silently
    letting the reader hit real googleapis.com.

    Duplicated from `examples/using-llamaindex-readers/_llamaindex.py:point_drive_at` (tests
    don't import from examples)."""
    from google.api_core.client_options import ClientOptions
    from googleapiclient import discovery

    base = base_url.rstrip("/")
    if not hasattr(discovery, "build"):
        raise RuntimeError(
            "point_drive_at: googleapiclient.discovery.build is gone — update the shim"
        )
    if getattr(discovery.build, "_points_at_mock", False):
        return

    _real_build = discovery.build

    def _build(*args, **kwargs):
        kwargs.setdefault("static_discovery", True)
        kwargs["client_options"] = ClientOptions(api_endpoint=f"{base}/drive/v3")
        return _real_build(*args, **kwargs)

    _build._points_at_mock = True
    discovery.build = _build


def test_gdrive(live_server):
    pytest.importorskip("llama_index.readers.google")
    from googleapiclient import discovery
    from llama_index.readers.google import GoogleDriveReader

    base, admin = _base_token(live_server)

    # Process-global patch (module attribute), so restore it after the test — left in place it'd
    # leak into later tests in the same session, e.g. test_sdk.py's drive/gmail cases calling the
    # real `googleapiclient.discovery.build` directly with their own `client_options` (this shim's
    # wrapper would clobber that with a stale base_url from this test's already-shut-down
    # live_server).
    _orig_build = discovery.build
    try:
        _point_drive_at(base)

        # `GoogleDriveReader.__init__` accepts `service_account_key` (a raw dict) directly —
        # an injection hook, so no monkeypatching of a private method is needed here (contrast
        # `GmailReader`, which has none). `_get_credentials()` turns it into a Credentials object
        # via `service_account.Credentials.from_service_account_info(self.service_account_key,
        # scopes=SCOPES)` — bare, no `subject` — so this path is admin-only (fine for this test;
        # `gdrive.py` documents the `subject`/impersonation gap and its workaround).
        sa_info = _google_service_account_info(base)
        reader = GoogleDriveReader(service_account_key=sa_info)

        # `load_data()` requires `folder_id` or `file_ids` (a bare `query_string` alone is a
        # no-op in this reader version — it only narrows results once one of those is given, see
        # `GoogleDriveReader.load_data`). "root" is the mock's synthetic root: `_get_fileids_meta`
        # recurses from it through every visible folder (marketing/finance/security) down to
        # their files, so this reaches the whole visible corpus without resolving a real folder id.
        docs = reader.load_data(folder_id="root")
        assert docs, "expected at least one Drive Document"
        assert any("palette" in d.text.lower() or "revenue" in d.text.lower() for d in docs)
    finally:
        discovery.build = _orig_build


def test_linear(live_server):
    """`LinearReader` hardcodes `graphql_endpoint` as a LOCAL VARIABLE inside `load_data`, so the
    only seam is the module's `requests` import — swapped here for a URL-rewriting proxy (the same
    thing `examples/.../_llamaindex.py:patch_linear_at` does; not imported, per the repo rule).

    The reader subscripts every `extra_info` field directly, so a missing one is a KeyError. That
    is the whole point of the test.

    The query filters to issues that HAVE an assignee and a project, and that is not incidental:
    `LinearReader` does `issue.get("assignee", {}).get("name", "")`, which raises
    `AttributeError: 'NoneType' object has no attribute 'get'` whenever the field is present and
    null — i.e. for every unassigned issue. `.get(k, default)` only returns the default when the
    key is ABSENT, and a GraphQL response always includes a selected field. This is a client-side
    bug that reproduces identically against real api.linear.app (Linear returns null for an
    unassigned issue too), so no mock-side change can fix it; the mock's `filter` support is the
    workaround, and `test_linear_reader_crashes_on_a_null_relation` below pins the diagnosis so a
    future reader release that fixes it is noticed.
    """
    pytest.importorskip("llama_index.readers.linear")
    import llama_index.readers.linear.base as lb
    from llama_index.readers.linear import LinearReader

    base, admin = _base_token(live_server)
    real_requests = lb.requests

    class _RequestsAtMock:
        def __getattr__(self, name):
            return getattr(real_requests, name)

        def post(self, url, *args, **kwargs):
            if url.startswith("https://api.linear.app"):
                url = url.replace("https://api.linear.app", f"{base}/linear")
            return real_requests.post(url, *args, **kwargs)

    lb.requests = _RequestsAtMock()
    try:
        # The caller supplies the document; this is the reader's own documented field set.
        query = """
        query Team {
          team(id: "ENG") {
            issues(filter: {assignee: {null: false}, project: {null: false}}) {
              nodes {
                id title description createdAt updatedAt archivedAt autoArchivedAt autoClosedAt
                branchName canceledAt completedAt dueDate estimate
                creator { name } assignee { name } state { name } project { name }
                labels { nodes { name } }
              }
            }
          }
        }
        """
        docs = LinearReader(api_key=admin).load_data(query)
    finally:
        lb.requests = real_requests

    assert docs, "expected at least one issue Document"
    # Every key the reader writes into extra_info must be present — no KeyError anywhere above.
    expected = {
        "id",
        "title",
        "created_at",
        "archived_at",
        "auto_archived_at",
        "auto_closed_at",
        "branch_name",
        "canceled_at",
        "completed_at",
        "creator",
        "due_date",
        "estimate",
        "labels",
        "project",
        "state",
        "updated_at",
        "assignee",
    }
    for d in docs:
        assert expected <= set(d.metadata)

    by_title = {d.metadata["title"]: d for d in docs}
    rl = by_title["Rate limiter drops bursts under 50ms"]
    assert "Token-bucket refill" in rl.text  # text is f"{title} \n {description}"
    assert rl.metadata["state"] == "In Progress"
    assert rl.metadata["assignee"] == "Bob Stone"
    assert rl.metadata["project"] == "runtime-stability"
    assert sorted(rl.metadata["labels"]) == ["bug", "gateway"]
    assert rl.metadata["estimate"] == 5
    assert rl.metadata["due_date"] == "2026-03-15"
    assert rl.metadata["branch_name"].endswith("eng-101-rate-limiter-drops-bursts-under-50ms")
    # The nullable lifecycle timestamps resolve as null rather than being absent.
    assert rl.metadata["archived_at"] is None and rl.metadata["canceled_at"] is None


def test_linear_acl_scoped_by_token(live_server):
    """The reader authenticates with a bare API key, so a user token must narrow what it reads."""
    pytest.importorskip("llama_index.readers.linear")
    import llama_index.readers.linear.base as lb
    import yaml
    from llama_index.readers.linear import LinearReader

    base, settings = live_server
    tokens = {
        u["email"]: u["token"] for u in yaml.safe_load(settings.tokens_path.read_text())["users"]
    }
    real_requests = lb.requests

    class _RequestsAtMock:
        def __getattr__(self, name):
            return getattr(real_requests, name)

        def post(self, url, *args, **kwargs):
            if url.startswith("https://api.linear.app"):
                url = url.replace("https://api.linear.app", f"{base}/linear")
            return real_requests.post(url, *args, **kwargs)

    query = (
        '{ team(id: "ENG") { issues(filter: {assignee: {null: false}}) '
        "{ nodes { id title description createdAt updatedAt "
        "archivedAt autoArchivedAt autoClosedAt branchName canceledAt completedAt dueDate "
        "estimate creator { name } assignee { name } state { name } project { name } "
        "labels { nodes { name } } } } } }"
    )
    lb.requests = _RequestsAtMock()
    try:
        titles = {
            t: {d.metadata["title"] for d in LinearReader(api_key=tok).load_data(query)}
            for t, tok in (("ava", tokens["ava@acme.com"]), ("hana", tokens["hana@acme.com"]))
        }
    finally:
        lb.requests = real_requests
    # lin-rl is public and assigned, so both see it; the restricted issue is unassigned, so it
    # is filtered out of both — scope the assertion to what the reader can actually load.
    assert "Rate limiter drops bursts under 50ms" in titles["ava"]
    assert "Rate limiter drops bursts under 50ms" in titles["hana"]


def test_linear_reader_crashes_on_a_null_relation(live_server):
    """Pins a CLIENT-side bug, so a reader release that fixes it is noticed rather than silently
    leaving the workaround in place.

    `LinearReader` does `issue.get("assignee", {}).get("name", "")`. A GraphQL response always
    includes a selected field, so an unassigned issue yields `assignee: null` — present, not
    absent — and `.get`'s default never applies. Real Linear answers the same way, so this is not
    something the mock can serve around; `test_linear` filters unassigned issues out instead.
    """
    pytest.importorskip("llama_index.readers.linear")
    import llama_index.readers.linear.base as lb
    from llama_index.readers.linear import LinearReader

    base, admin = _base_token(live_server)
    real_requests = lb.requests

    class _RequestsAtMock:
        def __getattr__(self, name):
            return getattr(real_requests, name)

        def post(self, url, *args, **kwargs):
            if url.startswith("https://api.linear.app"):
                url = url.replace("https://api.linear.app", f"{base}/linear")
            return real_requests.post(url, *args, **kwargs)

    lb.requests = _RequestsAtMock()
    try:
        # No filter: the corpus has unassigned issues, so the reader hits its own bug.
        with pytest.raises(AttributeError, match="NoneType"):
            LinearReader(api_key=admin).load_data(
                '{ team(id: "ENG") { issues { nodes { id title description createdAt updatedAt '
                "archivedAt autoArchivedAt autoClosedAt branchName canceledAt completedAt "
                "dueDate estimate creator { name } assignee { name } state { name } "
                "project { name } labels { nodes { name } } } } } }"
            )
    finally:
        lb.requests = real_requests
