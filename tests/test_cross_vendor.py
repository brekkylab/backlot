"""Assertions that span vendors, and so belong to no single router's file.

Over the conftest SAMPLE corpus (built into a tmp dir — hermetic, so the suite neither depends on
nor crawls whatever ambient import lives in ``data/``): a non-admin's crawl is a strict subset of
the admin's across every source at once, content round-trips byte-for-byte through each vendor's
encoding, and the ``/_mock/*`` affordances behave.

Each vendor's own endpoints live in ``test_<router>.py`` — the per-router crawlers those tests and
these share are in ``tests/_helpers.py``.
"""

from __future__ import annotations


import pytest

from backlot.config import Settings
from tests._helpers import build_corpus, client_for, crawl_confluence, db_count


def test_unauthenticated_request_reports_the_vendors_own_401_detail(client):
    """The message is part of the emulated surface — a client that string-matches its provider's
    error has to keep matching — which is why the shared guard takes it as a parameter.

    GitHub only: Google no longer goes through `auth.require_bearer`, because its answer is not one
    status (403 on Drive/Sheets, 401 on the OAuth-only families) and it carries Google's own error
    envelope, not `detail`. That surface is covered by the tests below."""
    r = client.get("/github/orgs/acme")
    assert r.status_code == 401
    assert r.json()["detail"] == "Bad credentials"


# --- ACL enforcement over HTTP --------------------------------------------------


def test_user_sees_subset_of_admin(client, admin_h, tokens_yaml, ro_conn, sample_settings):
    user = tokens_yaml["users"][0]
    uh = {"Authorization": f"Bearer {user['token']}"}
    admin_conf = len(crawl_confluence(client, admin_h))
    user_conf = len(crawl_confluence(client, uh))
    assert user_conf < admin_conf  # some confluence docs are group/private-restricted
    # matches exactly the ACL-computed visible count
    from backlot.acl import Acl

    acl = Acl.load(
        sample_settings.tokens_path, sample_settings.admin_token, sample_settings.org_name
    )
    vids = acl.visible_ids(ro_conn, acl.resolve(user["token"]))
    assert user_conf == db_count(ro_conn, "confluence", visible_ids=vids)


def test_mock_users_directory(client, tokens_yaml, org):
    # the /_mock/users directory lists every user + token (for testing per-user ACL)
    from backlot import synth

    body = client.get("/_mock/users").json()
    assert body["admin_token"] == tokens_yaml["admin_token"]
    # S3 uses an AWS keypair, not a token — the directory exposes an admin pair (derived from the
    # admin token, which is what the SigV4 verifier resolves) so a client can use it directly
    assert body["admin_s3_access_key_id"] == synth.s3_access_key_id(body["admin_token"])
    assert body["admin_s3_secret_access_key"] == synth.s3_secret_access_key(body["admin_token"])
    yaml_by_email = {u["email"]: u["token"] for u in tokens_yaml["users"]}
    assert body["count"] == len(body["users"]) == len(yaml_by_email) > 0
    for u in body["users"]:
        assert u["token"] == yaml_by_email[u["email"]]  # matches data/tokens_yaml.yaml
        assert u["name"] and isinstance(u["groups"], list)
        # each user also carries their derived S3 access-key/secret pair
        assert u["s3_access_key_id"] == synth.s3_access_key_id(u["token"])
        assert u["s3_secret_access_key"] == synth.s3_secret_access_key(u["token"])
    # a listed token really is ACL-scoped: it resolves and sees <= what admin sees
    u = body["users"][0]
    admin_repos = client.get(
        f"/github/orgs/{org}/repos", headers={"Authorization": f"Bearer {body['admin_token']}"}
    ).json()
    user_repos = client.get(
        f"/github/orgs/{org}/repos", headers={"Authorization": f"Bearer {u['token']}"}
    ).json()
    assert 0 < len(user_repos) <= len(admin_repos)


def test_mock_users_can_be_disabled(client, monkeypatch):
    from backlot import main

    monkeypatch.setattr(main, "get_settings", lambda: Settings(expose_tokens=False))
    assert client.get("/_mock/users").status_code == 404


def test_unauthenticated_is_rejected(client):
    # Drive accepts API keys, so an anonymous request is an "unregistered caller" -> 403, not 401.
    # A present-but-invalid bearer IS 401. Both measured; see the Google-envelope tests below.
    assert client.get("/drive/v3/files").status_code == 403
    assert (
        client.get("/drive/v3/files", headers={"Authorization": "Bearer nope"}).status_code == 401
    )
    assert client.get("/atlassian/rest/api/3/search/jql").status_code == 401
    slack = client.post("/slack/api/conversations.list").json()
    assert slack == {"ok": False, "error": "not_authed"}


# --- OpenAPI enrichment: the params each router advertises ---------------------------------
# The routers read query params off the raw request rather than through FastAPI signatures, so
# each has to declare what it honours by hand (openapi.qp). One table rather than a test per
# vendor: the assertion is identical and only the path and the expected names differ.


@pytest.mark.parametrize(
    "path, expected",
    [
        ("/github/search/issues", {"q", "page", "per_page"}),
        ("/slack/api/search.messages", {"query", "count", "page"}),
        ("/slack/api/conversations.history", {"channel", "limit", "cursor"}),
        # `user_id` is the path param, asserted here so enrichment cannot drop it
        ("/gmail/v1/users/{user_id}/messages", {"q", "maxResults", "pageToken", "user_id"}),
        ("/drive/v3/files", {"q", "pageSize", "pageToken", "fields"}),
        ("/notion/v1/users", {"start_cursor", "page_size"}),
        ("/atlassian/rest/api/3/search/jql", {"jql", "maxResults", "nextPageToken"}),
        ("/atlassian/wiki/rest/api/search", {"cql"}),
    ],
)
def test_router_advertises_the_params_it_honours(client, path, expected):
    op = client.get("/openapi.json").json()["paths"][path]["get"]
    assert expected <= {p["name"] for p in op.get("parameters", [])}


# --- /_mock/openapi/{source}: the MCP-ready spec endpoint (issue #4 bridge) ---------------


def test_mock_openapi_spec_endpoint(client):
    gh = client.get("/_mock/openapi/github")
    assert gh.status_code == 200
    ids = [
        op["operationId"]
        for item in gh.json()["paths"].values()
        for m, op in item.items()
        if isinstance(op, dict) and "operationId" in op
    ]
    assert ids and len(ids) == len(set(ids)), (
        "served spec must have unique operationIds (bridge-ready)"
    )
    assert client.get("/_mock/openapi/s3").status_code == 404  # SigV4 — intentionally no bridge
    assert client.get("/_mock/openapi/nope").status_code == 404


# --- /health: source_documents beside documents ---------------------------------------------


def _join_warm_thread(main_module) -> None:
    """Wait for the background per-source COUNT(*) cache (see lifespan._warm_caches) to land,
    deterministically — /health's `documents`/`by_source` are None/{} until it does, and a
    fresh-process cold start loses that race against an immediate request 100% of the time (no
    poll budget is safe on a slower CI runner), so the test has to join the thread, not wait on it."""
    warm = main_module.app.state.warm_thread
    warm.join(timeout=10)
    assert not warm.is_alive(), "cache warm-up did not finish within 10s"


def test_health_reports_both_counts(tmp_path):
    """Both numbers, always: the row count alone reads as inflated."""
    from backlot import main as main_module

    settings = build_corpus(
        tmp_path,
        [
            {
                "source_type": "slack",
                "channel": "incidents",
                "author_email": "bob@acme.com",
                "content": "Anyone seeing 502s?",
                "replies": [{"content": "Looking.", "author_email": "ava@acme.com"}],
            },
        ],
    )
    with client_for(settings, reload=True) as client:
        _join_warm_thread(main_module)
        body = client.get("/health").json()
    assert body["status"] == "ok"
    assert body["source_documents"] == 1
    assert body["documents"] == 2


def test_health_source_documents_is_null_without_the_meta_key(tmp_path):
    """A DB built before the meta table must still answer /health."""
    from backlot import main as main_module
    from backlot import store

    settings = build_corpus(
        tmp_path,
        [
            {
                "source_type": "confluence",
                "space": "h",
                "title": "A",
                "content": "a",
                "author_email": "ava@acme.com",
            },
        ],
    )
    conn = store.connect_rw(settings.db_path)
    conn.execute("DELETE FROM meta WHERE key = 'source_documents'")
    conn.commit()
    conn.close()
    with client_for(settings, reload=True) as client:
        _join_warm_thread(main_module)
        body = client.get("/health").json()
    assert body["source_documents"] is None
    assert body["documents"] == 1
