"""Surface (b): official clients that hardcode a vendor host, redirected at Backlot.

The URL helpers are pure and need no server. The monkeypatchers are checked for their observable
effect — a constructed client actually addressing the mock — because a shim that silently no-ops
would otherwise send a "mock" run to the real vendor.
"""

from __future__ import annotations

import pytest

import backlot


def test_url_helpers_are_pure_and_prefixed():
    from backlot.integrations import llamaindex as li

    base = "http://127.0.0.1:9999"
    assert li.slack_base_url(base) == f"{base}/slack/api/"  # trailing slash: slack_sdk joins
    assert li.github_base_url(base) == f"{base}/github"
    assert li.notion_base_url(base) == f"{base}/notion"
    assert li.s3_base_url(base) == f"{base}/s3"
    assert li.atlassian_base_url(base) == f"{base}/atlassian"
    assert li.linear_base_url(base) == f"{base}/linear"
    # A trailing slash on the input must not double up.
    assert li.github_base_url(base + "/") == f"{base}/github"


def test_mirage_slack_base_url_differs_from_the_llamaindex_one():
    """Not a copy-paste slip: slack_sdk appends the method to base_url, mirage does not."""
    from backlot.integrations import llamaindex as li
    from backlot.integrations import mirage as mg

    base = "http://127.0.0.1:9999"
    assert li.slack_base_url(base).endswith("/")
    assert not mg.slack_base_url(base).endswith("/")


def test_slack_reader_is_constructed_against_the_mock():
    pytest.importorskip("llama_index.readers.slack")
    from backlot.integrations.llamaindex import slack_reader_at

    with backlot.mock_server() as m:
        reader = slack_reader_at(m.base_url, m.token)
        assert m.base_url in str(reader._client.base_url)


def test_patch_notion_at_rebinds_every_hardcoded_host():
    pytest.importorskip("llama_index.readers.notion")
    import llama_index.readers.notion.base as nb

    from backlot.integrations.llamaindex import patch_notion_at

    with backlot.mock_server() as m:
        patch_notion_at(m.base_url)
        leaked = [
            n
            for n in dir(nb)
            if isinstance(getattr(nb, n), str) and "api.notion.com" in getattr(nb, n)
        ]
        assert leaked == [], f"still pointing at the real host: {leaked}"


def test_point_gmail_at_is_idempotent():
    pytest.importorskip("googleapiclient")
    from googleapiclient import discovery

    from backlot.integrations.llamaindex import point_gmail_at

    original = discovery.build
    try:
        point_gmail_at("http://127.0.0.1:9999")
        once = discovery.build
        point_gmail_at("http://127.0.0.1:9999")
        assert discovery.build is once, "second call re-wrapped an already-wrapped build"
    finally:
        discovery.build = original


def test_point_gmail_and_drive_at_each_redirect_their_own_service():
    """Regression: point_gmail_at and point_drive_at used to wrap the same
    googleapiclient.discovery.build behind one shared `_points_at_mock` flag, so whichever ran
    second found the flag already set and silently no-opped — leaving its service pointed at the
    OTHER function's endpoint. Confirmed failing on the pre-fix code: calling gmail@1111 then
    drive@2222 left `build("drive", ...)` resolving to 1111, Gmail's endpoint, not 2222/drive/v3.
    """
    pytest.importorskip("googleapiclient")
    from googleapiclient import discovery

    from backlot.integrations.llamaindex import point_drive_at, point_gmail_at

    calls = {}

    def _fake_real_build(service_name, version, **kwargs):
        calls[service_name] = kwargs["client_options"].api_endpoint
        return object()

    original = discovery.build
    discovery.build = _fake_real_build
    try:
        point_gmail_at("http://127.0.0.1:1111")
        point_drive_at("http://127.0.0.1:2222")

        discovery.build("gmail", "v1")
        discovery.build("drive", "v3")

        assert calls["gmail"] == "http://127.0.0.1:1111", calls
        assert calls["drive"] == "http://127.0.0.1:2222/drive/v3", calls
    finally:
        discovery.build = original


def test_google_build_registry_does_not_survive_a_direct_uninstall():
    """Regression: `_MOCK_SERVICE_ENDPOINTS` is a module-level dict that could outlive the wrapper
    reading it — unlike the old per-function closures, which were discarded whenever
    `discovery.build` was reset. Reproduction: point_gmail_at(A) installs the wrapper and
    registers "gmail" -> A; something resets `discovery.build` directly (exactly what this file's
    own tests do in `finally:` to undo a patch); point_drive_at(B) ALONE reinstalls the wrapper and
    registers "drive" -> B. Gmail must NOT still resolve to the stale A in this new round, since
    gmail was never touched in it.
    """
    pytest.importorskip("googleapiclient")
    from googleapiclient import discovery

    from backlot.integrations.llamaindex import point_drive_at, point_gmail_at

    calls = {}

    def _fake_real_build(service_name, version, **kwargs):
        calls[service_name] = kwargs.get("client_options")
        return object()

    original = discovery.build
    discovery.build = _fake_real_build
    try:
        point_gmail_at("http://127.0.0.1:1111")
        discovery.build = _fake_real_build  # direct uninstall, as this file's own tests do

        point_drive_at("http://127.0.0.1:2222")
        discovery.build("gmail", "v1")

        resolved = calls.get("gmail")
        endpoint = getattr(resolved, "api_endpoint", None)
        assert endpoint is None, (
            f"gmail should not be redirected in this round — it was never called after the "
            f"uninstall — but resolved via a stale registry entry: {endpoint!r}"
        )
    finally:
        discovery.build = original


def test_patch_linear_at_only_rewrites_linear_urls():
    pytest.importorskip("llama_index.readers.linear")
    import llama_index.readers.linear.base as lb

    from backlot.integrations.llamaindex import patch_linear_at

    real = lb.requests
    try:
        patch_linear_at("http://127.0.0.1:9999")
        assert lb.requests is not real
        # Anything that is not api.linear.app must pass through untouched.
        assert lb.requests.get is real.get
    finally:
        lb.requests = real
