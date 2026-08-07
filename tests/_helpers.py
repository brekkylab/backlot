"""Shared test machinery: build a corpus, serve it, and speak GraphQL to it.

Every HTTP test needs the same three steps — write records into a temp dir, point the app at that
dir, and drive it with a TestClient. The app reads ``BACKLOT_DATA_DIR`` through an ``lru_cache``d
``Settings``, so the cache has to be cleared on the way IN and again on the way OUT or a later test
module inherits this one's corpus. Eight copies of that dance lived in the test files, and the
env-restore half is exactly the part that is easy to get subtly wrong.

Deliberately NOT fixtures: most call sites need a client per corpus *inside* one test, not one
injected per test. ``tests/conftest.py`` still owns the fixtures over the shared ``SAMPLE`` corpus.
"""

from __future__ import annotations

import contextlib
import importlib
import json
import os
from pathlib import Path

from starlette.testclient import TestClient

from backlot.config import Settings, get_settings


def build_corpus(data_dir: Path, records: list[dict], *, name: str = "_corpus.jsonl") -> Settings:
    """Write ``records`` as a BYO-JSONL corpus under ``data_dir`` and load it into a fresh DB."""
    from backlot.importer.byo import load

    data_dir = Path(data_dir)
    data_dir.mkdir(parents=True, exist_ok=True)
    settings = Settings(data_dir=data_dir)
    corpus = data_dir / name
    corpus.write_text("\n".join(json.dumps(r) for r in records))
    load(corpus, settings)
    return settings


@contextlib.contextmanager
def client_for(settings: Settings, *, reload: bool = False):
    """A TestClient whose app is pointed at ``settings``, with the env restored on exit.

    ``reload=True`` re-imports ``backlot.main`` first. Needed only when a test opens a SECOND client
    over a different DB in the same session: the lifespan writes the connection and the reverse
    indexes onto the module-level ``app.state``, so a second lifespan start on the same object
    would overwrite the first client's state.
    """
    prev = os.environ.get("BACKLOT_DATA_DIR")
    os.environ["BACKLOT_DATA_DIR"] = str(settings.data_dir)
    get_settings.cache_clear()
    try:
        import backlot.main as main_module

        if reload:
            main_module = importlib.reload(main_module)
        with TestClient(main_module.app) as c:
            yield c
    finally:
        get_settings.cache_clear()
        if prev is None:
            os.environ.pop("BACKLOT_DATA_DIR", None)
        else:
            os.environ["BACKLOT_DATA_DIR"] = prev


def gql(client, path: str, query: str, token: str | None = None, **variables):
    """POST a GraphQL document and return the raw response.

    ``token`` goes onto Authorization VERBATIM — Linear accepts a scheme-less API key, so the
    caller decides whether to prefix ``Bearer`` and a test can assert on either spelling. The
    response, not the parsed body, because several tests assert on the status code.
    """
    body: dict = {"query": query}
    if variables:
        body["variables"] = variables
    headers = {"Authorization": token} if token is not None else {}
    return client.post(path, json=body, headers=headers)


def db_count(conn, source_type, **kw) -> int:
    """The stored row count a crawl's completeness assertion is checked against."""
    from backlot import store

    return store.count_documents(conn, source_type, **kw)


def tok(tokens_yaml, email: str) -> str:
    """One user's bearer token out of ``tokens.yaml``."""
    return next(u["token"] for u in tokens_yaml["users"] if u["email"] == email)


def tiny_corpus(tmp_path, records):
    """One small corpus in ``tmp_path``. Many shape tests call a router's response builder against
    the resulting rows rather than over HTTP, so they want the settings, not a client."""
    return build_corpus(tmp_path, records, name="corpus.jsonl")


@contextlib.contextmanager
def corpus_client(tmp_path, records):
    """``records`` built and served, yielding ``(client, settings)`` — the GraphQL tests need the
    admin token off the settings alongside the client."""
    settings = tiny_corpus(tmp_path, records)
    with client_for(settings) as client:
        yield client, settings


def bare_request():
    """A minimal Starlette Request, for response builders that only read the URL."""
    from starlette.requests import Request

    return Request(
        {
            "type": "http",
            "headers": [],
            "query_string": b"",
            "scheme": "http",
            "server": ("mock", 80),
            "path": "/",
        }
    )


def epoch_of(iso: str) -> int:
    """An ISO-8601 string as unix seconds."""
    from datetime import datetime

    return int(datetime.fromisoformat(iso.replace("Z", "+00:00")).timestamp())


# --- per-vendor crawlers -----------------------------------------------------------------------
# Small page sizes on purpose, so each one exercises its vendor's pagination. Shared because two
# kinds of test need them: each provider's "an admin crawl reaches every stored document", and the
# cross-cutting check that a non-admin's crawl is a strict subset of the admin's.


def crawl_slack(client, headers):
    total, cursor = 0, None
    channels = []
    while True:
        data = {"limit": 8}
        if cursor:
            data["cursor"] = cursor
        j = client.post("/slack/api/conversations.list", headers=headers, data=data).json()
        channels += j["channels"]
        cursor = j["response_metadata"]["next_cursor"]
        if not cursor:
            break
    for ch in channels:
        ccur = None
        while True:
            d = {"channel": ch["id"], "limit": 50}
            if ccur:
                d["cursor"] = ccur
            h = client.post("/slack/api/conversations.history", headers=headers, data=d).json()
            for m in h["messages"]:
                total += 1
                if m.get(
                    "reply_count"
                ):  # a thread root — its replies come from conversations.replies
                    r = client.post(
                        "/slack/api/conversations.replies",
                        headers=headers,
                        data={"channel": ch["id"], "ts": m["ts"]},
                    ).json()
                    total += len(r["messages"]) - 1  # thread includes the root we already counted
            ccur = h["response_metadata"]["next_cursor"]
            if not ccur:
                break
    return total


def crawl_hubspot(client, headers, object_type, limit=2, archived=False):
    """Cursor-paginate one CRM object type. Terminates on the ABSENCE of paging.next — which is
    exactly how the official client's fetch_all decides it is done, so a mock that always emits
    paging.next would hang a real client rather than error."""
    out, after = [], None
    while True:
        params = {"limit": limit}
        if archived:
            params["archived"] = "true"
        if after:
            params["after"] = after
        j = client.get(
            f"/hubspot/crm/v3/objects/{object_type}", headers=headers, params=params
        ).json()
        out += j["results"]
        nxt = (j.get("paging") or {}).get("next")
        if not nxt:
            break
        after = nxt["after"]
    return out


# --- crawlers (small page sizes to exercise pagination) -------------------------


def crawl_gmail(client, headers, user="me"):
    ids, token = [], None
    while True:
        p = {"maxResults": 7}
        if token:
            p["pageToken"] = token
        j = client.get(f"/gmail/v1/users/{user}/messages", headers=headers, params=p).json()
        ids += [m["id"] for m in j.get("messages", [])]
        token = j.get("nextPageToken")
        if not token:
            break
    return ids


def crawl_drive(client, headers):
    ids, token = [], None
    while True:
        p = {"pageSize": 7}
        if token:
            p["pageToken"] = token
        j = client.get("/drive/v3/files", headers=headers, params=p).json()
        ids += [f["id"] for f in j.get("files", [])]
        token = j.get("nextPageToken")
        if not token:
            break
    return ids


def crawl_github_repo(client, headers, org, repo):
    out, page = [], 1
    while True:
        r = client.get(
            f"/github/repos/{org}/{repo}/issues",
            headers=headers,
            params={"per_page": 5, "page": page, "state": "all"},
        )
        body = r.json()
        out += body
        if 'rel="next"' not in r.headers.get("Link", ""):
            break
        page += 1
    return out


def crawl_jira(client, headers):
    out, token = [], None
    while True:
        p = {"maxResults": 6}
        if token:
            p["nextPageToken"] = token
        j = client.get("/atlassian/rest/api/3/search/jql", headers=headers, params=p).json()
        out += j["issues"]
        if j.get("isLast", True):
            break
        token = j["nextPageToken"]
    return out


def crawl_confluence(client, headers):
    out, start, limit = [], 0, 7
    while True:
        j = client.get(
            "/atlassian/wiki/rest/api/content",
            headers=headers,
            params={"start": start, "limit": limit, "expand": "body.storage"},
        ).json()
        out += j["results"]
        if "next" not in j.get("_links", {}):
            break
        start += limit
    return out
