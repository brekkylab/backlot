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
import re
from pathlib import Path

from starlette.testclient import TestClient

from backlot.config import Settings, get_settings


# A value for every field a schema can ask for, so `complete` can answer any of them. A field
# missing here raises rather than guesses, which is how a new requirement announces itself.
_FIELD_VALUES = {
    "content": "Body text.",
    "title": "A title",
    "created": "2026-02-10T18:00:00Z",
    "updated": "2026-02-11T09:00:00Z",
    "author_email": "ava@acme.com",
    "key": "runbooks/oncall.md",
    "status": "In Progress",
    "issuetype": "Task",
    "reporter": "bob@acme.com",
    "state": "open",
    "identifier": "ENG-1",
    "properties": {"name": "Acme Health"},
    "duration": 30.0,
    "host_email": "ava@acme.com",
}
_CONTAINER_VALUES = {
    "slack": "incidents",
    "gmail": "ops",
    "google_drive": "runbooks",
    "github": "gateway",
    "jira": "payments",
    "confluence": "handbook",
    "notion": "engineering",
    "s3": "eng-artifacts",
    "hubspot": "companies",
    "linear": "engineering",
    "fireflies": "all-hands",
}
# A required-property error about the RECORD, not about something inside it: a nested one carries
# the path it is about in brackets ("… [associations/0]: 'to' is a required property"), and reading
# that as a field of the record would have `complete` inventing an `associations` key called `to`.
_REQUIRED_RE = re.compile(r"^[^\[\]]*: '([^']+)' is a required property")


def _identifier_for(rec: dict) -> str:
    """A Linear identifier for a record that did not state one.

    The PREFIX comes from the team and the number from the document, because Linear's key space is
    per-team: one prefix may not span two teams, and two issues in one team may not share a number.
    A constant would break whichever rule the test was not about.
    """
    import hashlib

    team = "".join(ch for ch in str(rec.get("team") or "team") if ch.isalnum())
    seed = str(rec.get("doc_id") or rec.get("title") or "")
    number = int(hashlib.sha256(seed.encode()).hexdigest()[:6], 16) % 9000 + 1
    return f"{(team[:3] or 'TEA').upper()}-{number}"


def _seconds_after(when, offset: int):
    """``when`` plus ``offset`` seconds, in whichever form ``when`` was written."""
    from datetime import datetime, timedelta, timezone

    # Epoch seconds in either spelling: a corpus may write them as a number or as a string, and the
    # answer comes back the way the question was asked.
    if isinstance(when, (int, float)):
        return int(when) + offset
    text = str(when).strip()
    if text.lstrip("-").isdigit():
        return str(int(text) + offset)
    stamp = datetime.fromisoformat(text.replace("Z", "+00:00")) + timedelta(seconds=offset)
    return stamp.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def complete(source_type: str, _omit: frozenset[str] | set[str] = frozenset(), **overrides) -> dict:
    """A record that states everything its schema asks for, so a test can say only what it is about.

    Read off the schema rather than from a list kept here: a helper carrying its own copy of the
    required fields would keep handing out records that pass while the contract moved underneath it.
    The tests that are ABOUT the contract build their records by hand instead -- see
    ``tests/test_schema.py``.

    ``_omit`` names fields to leave unstated, for a test whose subject IS one missing field: the
    record is complete except for that, so the only error is the one being examined.
    """
    from backlot import store
    from backlot.validation import record_errors

    # A child row states its own author and second. Filled from the root's clock, one second apart,
    # so a thread the test did not date still reads in the order it was written.
    overrides = dict(overrides)
    root_second = overrides.get("created") or _FIELD_VALUES["created"]
    # The spacing each child array has: a slack reply lands a second after the message before it, a
    # gmail thread message an hour later, a comment a minute. Matching the source keeps a test that
    # asserts on the served time reading the number its source implies.
    for array, time_field, step in (
        ("comments", "created_ts", 60),
        ("replies", "created", 1),
        ("messages", "created", 3600),
        ("sentences", "start_time", 0),
    ):
        children = overrides.get(array)
        if not isinstance(children, list):
            continue
        filled = []
        for i, child in enumerate(children, start=1):
            if not isinstance(child, dict):
                filled.append(child)
                continue
            child = dict(child)
            if array == "sentences":
                child.setdefault(time_field, i - 1)
            else:
                child.setdefault("author_email", _FIELD_VALUES["author_email"])
                child.setdefault(time_field, _seconds_after(root_second, i * step))
            filled.append(child)
        overrides[array] = filled

    rec: dict = {"source_type": source_type}
    container = store.grouping_col(source_type)
    for _ in range(24):
        merged = {**rec, **overrides}
        missing = [m.group(1) for e in record_errors(merged) if (m := _REQUIRED_RE.search(e))]
        if not missing:
            return merged
        missing = [f for f in missing if f not in _omit]
        if not missing:
            return merged
        for field in missing:
            if field == container:
                rec[field] = _CONTAINER_VALUES[source_type]
            elif field == "identifier":
                rec[field] = _identifier_for(merged)
            elif field in _FIELD_VALUES:
                rec[field] = _FIELD_VALUES[field]
            else:
                raise AssertionError(
                    f"{source_type} now requires {field!r}; teach tests._helpers._FIELD_VALUES "
                    f"a value for it"
                )
    raise AssertionError(f"{source_type}: {record_errors({**rec, **overrides})}")


def served_id(source_type: str, seed: str):
    """The id a row with corpus ``doc_id == seed`` lands under, computed rather than looked up.

    A corpus's own identifier does not outlive the import, so a test cannot ask the DB
    "which row was `dsid_1`?". For the sources whose served id is a pure function of that
    identifier it does not need to: the same seed gives the same answer here as it did at import,
    which is what makes the value stable in the first place.

    Valid for gmail, google_drive, notion, linear, fireflies (never probed) and for confluence and
    hubspot (probed, but the probe only moves a row off its seed on a collision, which a
    fixture-sized corpus does not produce). NOT valid for github, jira or slack, whose key is
    assigned against the whole corpus — a test needing one of those reads it back off the row.
    """
    from backlot import store

    return store.id_seed(source_type)(seed)


def build_corpus(
    data_dir: Path, records: list[dict], *, name: str = "_corpus.jsonl", raw: bool = False
) -> Settings:
    """Write ``records`` as a BYO-JSONL corpus under ``data_dir`` and load it into a fresh DB.

    Each record is completed against its schema first (see :func:`complete`), so a test states only
    the fields it is about. ``raw=True`` writes them as given, for the tests about refusal itself.
    """
    from backlot.importer.byo import load

    data_dir = Path(data_dir)
    data_dir.mkdir(parents=True, exist_ok=True)
    settings = Settings(data_dir=data_dir)
    corpus = data_dir / name
    if not raw:
        records = [
            complete(**r) if isinstance(r, dict) and "source_type" in r else r for r in records
        ]
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


def selected_fields(document: str, *path: str) -> set[str]:
    """The field names ``document`` selects at ``path`` — for asserting on a generated selection
    set (``backlot.graphql.mcp_tools``) by shape rather than by substring."""
    from graphql import parse

    node = parse(document).definitions[0]
    for step in path:
        node = next(s for s in node.selection_set.selections if s.name.value == step)
    return {s.name.value for s in node.selection_set.selections}


def selected_field_count(document: str) -> int:
    """How many field nodes ``document`` selects, at every level."""
    from graphql import parse

    def walk(node) -> int:
        selections = getattr(node, "selection_set", None)
        return 0 if selections is None else sum(1 + walk(s) for s in selections.selections)

    return walk(parse(document).definitions[0])


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
# kinds of test need them: each source's "an admin crawl reaches every stored document", and the
# cross-cutting check that a non-admin's crawl is a strict subset of the admin's.


def crawl_slack(client, headers):
    total, cursor = 0, None
    channels = []
    while True:
        # Both channel types, because Slack's own default is `public_channel` alone — a crawler
        # that omits `types` reaches no private channel, on the mock or on the real API.
        data = {"limit": 8, "types": "public_channel,private_channel"}
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
            # Terminates on the ABSENCE of response_metadata, not on an empty cursor inside it:
            # conversations.history omits the key entirely on a last page, so a client that
            # subscripts it unconditionally raises against real Slack.
            ccur = (h.get("response_metadata") or {}).get("next_cursor")
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
