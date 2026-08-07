"""Surface (a): a server from the installed package, with no arguments and no checkout."""

from __future__ import annotations

import json
import urllib.request

import backlot


def _get(url: str) -> dict:
    with urllib.request.urlopen(url, timeout=10) as r:
        return json.load(r)


def test_mock_server_with_no_arguments_serves_the_hello_corpus():
    with backlot.mock_server() as m:
        body = _get(f"{m.base_url}/health")
    assert body["status"] == "ok"
    assert body["source_documents"] > 0
    assert body["documents"] >= body["source_documents"]


def test_mock_server_accepts_records():
    with backlot.mock_server(
        [
            {
                "source_type": "confluence",
                "space": "handbook",
                "title": "Only Page",
                "content": "The only document.",
                "author_email": "ava@acme.com",
            },
        ]
    ) as m:
        body = _get(f"{m.base_url}/health")
    assert body["source_documents"] == 1


def test_mock_server_token_authenticates():
    with backlot.mock_server() as m:
        req = urllib.request.Request(
            f"{m.base_url}/slack/api/auth.test", headers={"Authorization": f"Bearer {m.token}"}
        )
        with urllib.request.urlopen(req, timeout=10) as r:
            assert json.load(r)["ok"] is True


def test_two_servers_get_different_ports():
    with backlot.mock_server() as a, backlot.mock_server() as b:
        assert a.base_url != b.base_url


def test_serve_or_connect_falls_back_when_the_url_is_unreachable():
    with backlot.serve_or_connect(url="http://127.0.0.1:1/") as m:
        assert _get(f"{m.base_url}/health")["status"] == "ok"
