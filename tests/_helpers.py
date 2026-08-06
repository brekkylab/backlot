"""Shared test machinery: build a corpus, serve it, and speak GraphQL to it.

Every HTTP test needs the same three steps — write records into a temp dir, point the app at that
dir, and drive it with a TestClient. The app reads ``MOCK_DATA_DIR`` through an ``lru_cache``d
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

from app.config import Settings, get_settings


def build_corpus(data_dir: Path, records: list[dict], *, name: str = "_corpus.jsonl") -> Settings:
    """Write ``records`` as a BYO-JSONL corpus under ``data_dir`` and load it into a fresh DB."""
    from app.importer.byo import load

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

    ``reload=True`` re-imports ``app.main`` first. Needed only when a test opens a SECOND client
    over a different DB in the same session: the lifespan writes the connection and the reverse
    indexes onto the module-level ``app.state``, so a second lifespan start on the same object
    would overwrite the first client's state.
    """
    prev = os.environ.get("MOCK_DATA_DIR")
    os.environ["MOCK_DATA_DIR"] = str(settings.data_dir)
    get_settings.cache_clear()
    try:
        import app.main as main_module

        if reload:
            main_module = importlib.reload(main_module)
        with TestClient(main_module.app) as c:
            yield c
    finally:
        get_settings.cache_clear()
        if prev is None:
            os.environ.pop("MOCK_DATA_DIR", None)
        else:
            os.environ["MOCK_DATA_DIR"] = prev


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
    from app import store

    return store.count_documents(conn, source_type, **kw)


def tok(tokens_yaml, email: str) -> str:
    """One user's bearer token out of ``tokens.yaml``."""
    return next(u["token"] for u in tokens_yaml["users"] if u["email"] == email)
