"""Surface (b): official clients that hardcode a vendor host, redirected at Backlot.

What lives in ``backlot.integrations`` is only what a caller cannot write themselves: the
monkeypatchers, and the one reader that must be built already-redirected. A client that takes a base
URL as an argument gets ``f"{s.base_url}/<prefix>"`` at the call site instead — see the examples.

The patchers are checked for their observable effect — a constructed client actually addressing the
mock — because a shim that silently no-ops would otherwise send a "mock" run to the real vendor.
"""

from __future__ import annotations

import sys

import pytest

import backlot

# Every constant point_google_at / point_github_at rebind. Spelled out here rather than read back
# out of the functions, so the completeness test below cannot agree with the code by construction.
_NAMED_BY_THE_PATCHERS = {
    "TOKEN_URL",
    "GMAIL_API_BASE",
    "DRIVE_API_BASE",
    "DRIVE_UPLOAD_BASE",
    "DOCS_API_BASE",
    "SHEETS_API_BASE",
    "SLIDES_API_BASE",
    "CALENDAR_API_BASE",
    "FORMS_API_BASE",
    "API_BASE",
}


def test_slack_reader_is_constructed_against_the_mock():
    pytest.importorskip("llama_index.readers.slack")
    from backlot.integrations.llamaindex import slack_reader_at

    with backlot.serve() as s:
        reader = slack_reader_at(s.base_url, s.token)
        built = str(reader._client.base_url)
        assert s.base_url in built
        # The TRAILING SLASH is the whole reason this URL is built inside the helper rather than
        # passed in: slack_sdk joins `base_url + method`, so without it every call would address
        # `/slackconversations.history`.
        assert built.endswith("/slack/api/"), built


def test_patch_notion_at_rebinds_every_hardcoded_host():
    pytest.importorskip("llama_index.readers.notion")
    import llama_index.readers.notion.base as nb

    from backlot.integrations.llamaindex import patch_notion_at

    with backlot.serve() as s:
        patch_notion_at(s.base_url)
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
    """point_gmail_at and point_drive_at must not each wrap the same
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
    """`_MOCK_SERVICE_ENDPOINTS` is a module-level dict that can outlive the wrapper
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


def _isolate_mirage(monkeypatch, modules: dict[str, dict[str, str]]):
    """Replace every imported ``mirage*`` module with the given stand-ins, for this test only.

    The real package is installed, so without evicting it the constant sweep would find genuine
    ``mirage.core.*`` modules alongside a stand-in and report a patch that the stand-in never got —
    making the assertion depend on which test imported mirage first.
    """
    import types

    for name in [n for n in sys.modules if n == "mirage" or n.startswith("mirage.")]:
        monkeypatch.delitem(sys.modules, name)
    for name in ("mirage", "mirage.core", "mirage.core.google", "mirage.core.github"):
        monkeypatch.setitem(sys.modules, name, types.ModuleType(name))
    made = {}
    for name, constants in modules.items():
        mod = types.ModuleType(name)
        for const, value in constants.items():
            setattr(mod, const, value)
        monkeypatch.setitem(sys.modules, name, mod)
        made[name] = mod
    return made


def test_mirage_patchers_raise_when_a_constant_is_renamed(monkeypatch):
    """A sweep of `if hasattr: setattr` with no counter means a mirage upgrade that
    renamed a constant made both patchers return successfully having changed nothing — and the run
    then addressed gmail.googleapis.com / api.github.com with the caller's real credentials.
    `integrations/__init__.py` promises the opposite ("fails loudly if its seam disappears").
    """
    from backlot.integrations.mirage import point_github_at, point_google_at

    mods = _isolate_mirage(
        monkeypatch,
        {
            "mirage.core.google._client": {
                "GMAIL_BASE_URL": "https://gmail.googleapis.com/gmail/v1"
            },
            "mirage.core.github._client": {"GITHUB_API_ROOT": "https://api.github.com"},
        },
    )
    for fn in (point_google_at, point_github_at):
        with pytest.raises(RuntimeError, match="mirage's constants moved"):
            fn("http://127.0.0.1:9999")
    # and nothing was left half-patched at the real hosts' expense
    for mod in mods.values():
        for name, val in vars(mod).items():
            if name.isupper():
                assert "127.0.0.1" not in val, f"{name} was rebound despite the raise"


def test_mirage_patchers_rebind_every_constant_they_name(monkeypatch):
    """The success path, against the constants mirage actually ships: every name each patcher
    declares must land, since the raise above is only as good as the set it checks."""
    from backlot.integrations.mirage import point_github_at, point_google_at

    real = pytest.importorskip("mirage.core.google._client")
    real_github = pytest.importorskip("mirage.core.github._client")
    google_names = [n for n in vars(real) if n.isupper() and isinstance(getattr(real, n), str)]
    github_names = [
        n for n in vars(real_github) if n.isupper() and isinstance(getattr(real_github, n), str)
    ]

    mods = _isolate_mirage(
        monkeypatch,
        {
            "mirage.core.google._client": {n: getattr(real, n) for n in google_names},
            "mirage.core.github._client": {n: getattr(real_github, n) for n in github_names},
        },
    )
    point_google_at("http://127.0.0.1:9999")
    point_github_at("http://127.0.0.1:9999")

    patched = {
        n
        for mod in mods.values()
        for n, v in vars(mod).items()
        if n.isupper() and isinstance(v, str) and "127.0.0.1" in v
    }
    assert patched == _NAMED_BY_THE_PATCHERS, (
        f"missing: {sorted(_NAMED_BY_THE_PATCHERS - patched)}, unexpected: {sorted(patched - _NAMED_BY_THE_PATCHERS)}"
    )


def test_mirage_patchers_name_every_host_constant_mirage_ships(monkeypatch):
    """A constant mirage adds later is one the patchers do not know about, so it keeps pointing at
    the real vendor — silently, because the raise above only covers names we already name. Fails
    when mirage grows one, which is the moment to decide whether the mock serves it.
    """
    real = pytest.importorskip("mirage.core.google._client")
    real_github = pytest.importorskip("mirage.core.github._client")

    from backlot.integrations import mirage as shim

    unpatched = {}
    for mod in (real, real_github):
        for name, val in vars(mod).items():
            if not (name.isupper() and isinstance(val, str) and val.startswith("http")):
                continue
            if name not in _NAMED_BY_THE_PATCHERS:
                unpatched[f"{mod.__name__}.{name}"] = val
    assert unpatched == {}, (
        f"mirage ships host constants {shim.__name__} does not rebind, so they stay pointed at the "
        f"real vendor: {unpatched}"
    )


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
