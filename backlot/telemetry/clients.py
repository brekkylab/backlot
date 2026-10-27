"""The client library a request's headers name, sent as ``backlot.client``."""

from __future__ import annotations

import re
from typing import Mapping, NamedTuple


class _Rule(NamedTuple):
    header: str
    # `{v}` in the label is the major.minor the pattern captures.
    pattern: str
    label: str
    # The headers a client sent when this rule was measured; tests hold the rule to them.
    sent: Mapping[str, str]


# First match wins. Measured on macOS 15 arm64: Backlot's examples run through a header-recording
# proxy on 2026-10-14 (Python 3.12.12, Node 24), and curl, httplib2 and a bare botocore client
# sent on their own on 2026-10-20 (Python 3.15.0).
_CLIENTS = [
    # examples/using-official-sdk/gmail.py and gdrive.py
    _Rule(
        "x-goog-api-client",
        r"\bgdcl/(\d+)\.(\d+)",
        "google-api-python-client/{v}",
        {"user-agent": "(gzip)", "x-goog-api-client": "gdcl/2.200.0 gl-python/3.12.12 cred-type/u"},
    ),
    # The same examples' token refresh.
    _Rule(
        "x-goog-api-client",
        r"\bauth/(\d+)\.(\d+)",
        "google-auth/{v}",
        {
            "user-agent": "Python-httplib2/0.32.0 (gzip)",
            "x-goog-api-client": "gl-python/3.12.12 auth/2.57.1 cred-type/u",
        },
    ),
    # examples/using-official-sdk/s3.py
    _Rule(
        "user-agent",
        r"\bBoto3/(\d+)\.(\d+)",
        "boto3/{v}",
        {
            "user-agent": "Boto3/1.40.61 md/Botocore#1.40.61 ua/2.1 os/macos#24.6.0 md/arch#arm64 "
            "lang/python#3.12.12 md/pyimpl#CPython m/b,N,Z,D,e cfg/retry-mode#legacy "
            "Botocore/1.40.61"
        },
    ),
    # examples/using-llamaindex-readers/s3.py, using-fsspec/s3.py and using-mirage/s3.py
    _Rule(
        "user-agent",
        r"\baiobotocore/(\d+)\.(\d+)",
        "aiobotocore/{v}",
        {
            "user-agent": "aiobotocore/2.25.1 md/Botocore#1.40.61 ua/2.1 os/macos#24.6.0 "
            "md/arch#arm64 lang/python#3.12.12 md/pyimpl#CPython m/Z,D,e,N,C,b "
            "cfg/retry-mode#legacy botocore/1.40.61"
        },
    ),
    # A botocore client of its own.
    _Rule(
        "user-agent",
        r"\b[Bb]otocore/(\d+)\.(\d+)",
        "botocore/{v}",
        {
            "user-agent": "Botocore/1.40.61 ua/2.1 os/macos#24.6.0 md/arch#arm64 "
            "lang/python#3.15.0 md/pyimpl#CPython m/b,e,Z,D,N cfg/retry-mode#legacy"
        },
    ),
    # examples/using-official-sdk/slack.py and using-llamaindex-readers/slack.py
    _Rule(
        "user-agent",
        r"\bslackclient/(\d+)\.(\d+)",
        "slack_sdk/{v}",
        {"user-agent": "Python/3.12.12 slackclient/3.44.1 Darwin/24.6.0"},
    ),
    # examples/using-official-sdk/hubspot.py
    _Rule(
        "user-agent",
        r"\bhubspot-api-client-python; (\d+)\.(\d+)",
        "hubspot-api-client/{v}",
        {"user-agent": "hubspot-api-client-python; 12.0.0"},
    ),
    # examples/using-official-sdk/notion.py
    _Rule(
        "user-agent",
        r"\bnotion-sdk-py@(\d+)\.(\d+)",
        "notion-client/{v}",
        {"user-agent": "ramnes/notion-sdk-py@3.1.0"},
    ),
    # examples/using-official-sdk/github.py
    _Rule("user-agent", r"^PyGithub/", "PyGithub", {"user-agent": "PyGithub/Python"}),
    # examples/using-mirage/slack.py
    _Rule(
        "user-agent",
        r"\baiohttp/(\d+)\.(\d+)",
        "aiohttp/{v}",
        {"user-agent": "Python/3.12 aiohttp/3.14.3"},
    ),
    # backlot mcp
    _Rule(
        "user-agent",
        r"^python-httpx2/(\d+)\.(\d+)",
        "python-httpx2/{v}",
        {"user-agent": "python-httpx2/2.12.0"},
    ),
    # examples/using-official-sdk/msteams.py (msgraph-sdk), which sends its own `SdkVersion` only to
    # graph.microsoft.com: what names it to any other host is the Kiota runtime its requests run on.
    # Measured 2026-10-21 (msgraph-sdk 1.64.0).
    _Rule(
        "user-agent",
        r"\bkiota-python/(\d+)\.(\d+)",
        "kiota-python/{v}",
        {"user-agent": "python-httpx/0.28.1 kiota-python/1.14.2"},
    ),
    # examples/using-official-sdk/fireflies.py and using-llamaindex-readers/github.py
    _Rule(
        "user-agent",
        r"^python-httpx/(\d+)\.(\d+)",
        "python-httpx/{v}",
        {"user-agent": "python-httpx/0.28.1"},
    ),
    # examples/using-official-sdk/jira.py and confluence.py, using-llamaindex-readers/linear.py and
    # using-fsspec/github.py
    _Rule(
        "user-agent",
        r"^python-requests/(\d+)\.(\d+)",
        "python-requests/{v}",
        {"user-agent": "python-requests/2.34.2"},
    ),
    # backlot mcp
    _Rule(
        "user-agent",
        r"^Python-urllib/(\d+)\.(\d+)",
        "python-urllib/{v}",
        {"user-agent": "Python-urllib/3.12"},
    ),
    # An httplib2 client of its own.
    _Rule(
        "user-agent",
        r"^Python-httplib2/(\d+)\.(\d+)",
        "httplib2/{v}",
        {"user-agent": "Python-httplib2/0.32.0 (gzip)"},
    ),
    # curl 8.7.1
    _Rule("user-agent", r"^curl/(\d+)\.(\d+)", "curl/{v}", {"user-agent": "curl/8.7.1"}),
    # examples/using-official-sdk/linear/index.ts (@linear/sdk)
    _Rule("user-agent", r"^node$", "node", {"user-agent": "node"}),
]


def client(headers: Mapping[str, str]) -> str:
    """The library a request's headers name; ``other`` when none of the rules matches."""
    for rule in _CLIENTS:
        m = re.search(rule.pattern, headers.get(rule.header, ""))
        if m:
            return rule.label.format(v=".".join(m.groups())) if m.groups() else rule.label
    return "other"
