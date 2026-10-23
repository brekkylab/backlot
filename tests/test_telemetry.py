"""Usage telemetry: when it is on, how a call is named, what an export carries, and what it never
carries. Served over the bundled hello corpus, with an export endpoint nothing listens on unless a
test starts one."""

from __future__ import annotations

import base64
import gzip
import json
import os
import re
import socket
import threading
import time
import zlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from backlot import store
from backlot.config import get_settings
from backlot.server import HELLO_CORPUS
from backlot.telemetry import clients, enabled, errors, naming, notice, recorder
from backlot.telemetry.errors import error_class
from backlot.telemetry.middleware import _credential
from backlot.telemetry.recorder import MAX_SERIES, RECORDER, Exporter, Recorder, encode
from tests._helpers import build_corpus, client_for

ADMIN = {"Authorization": "Bearer admin-service-token"}
LINEAR = {"Authorization": "admin-service-token"}
JSON = {"Content-Type": "application/json"}
UNREACHABLE = "http://127.0.0.1:9/v1/metrics"
# The values an export may carry. The collector replaces a value outside its own shape checks with
# `invalid`, and every pattern here is narrower than those.
STORED = {
    "backlot.source": r"^(github|slack|gmail|google_drive|jira|confluence|notion|s3|hubspot|linear|fireflies|msteams|other)$",
    "backlot.support": r"^(implemented|extra|refused|missing|unknown)$",
    "backlot.operation": r"^[A-Za-z_][A-Za-z0-9_.,/+{}-]{0,127}$",
    "backlot.error_code": r"^[A-Za-z][A-Za-z0-9_.-]{0,63}$",
    "backlot.client": r"^[A-Za-z0-9@_.-]{1,48}(/[0-9]+[.][0-9]+)?$",
    "backlot.error_class": r"^(client|auth|rate_limit|unsupported|internal)$",
}


@pytest.fixture(scope="module")
def hello(tmp_path_factory):
    records = [
        json.loads(line)
        for line in HELLO_CORPUS.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    return build_corpus(tmp_path_factory.mktemp("hello"), records, raw=True)


def _env(**values):
    """Set environment variables for a block, restoring what was there."""
    saved = {k: os.environ.get(k) for k in values}

    class _Restore:
        def __enter__(self):
            for k, v in values.items():
                if v is None:
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = v

        def __exit__(self, *exc):
            for k, v in saved.items():
                if v is None:
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = v

    return _Restore()


class _ExportTo:
    """Point the server's exporter at ``url``, exporting only at shutdown."""

    def __init__(self, url):
        self.url = url

    def __enter__(self):
        self.saved = recorder.ENDPOINT, recorder.INTERVAL
        recorder.ENDPOINT, recorder.INTERVAL = self.url, 3600.0

    def __exit__(self, *exc):
        recorder.ENDPOINT, recorder.INTERVAL = self.saved


@pytest.fixture(scope="module")
def client(hello):
    with _env(BACKLOT_TELEMETRY="1", DO_NOT_TRACK=None), _ExportTo(UNREACHABLE):
        with client_for(hello) as c:
            yield c


def _calls(send) -> list[dict]:
    """The calls recorded while ``send`` ran, one dict per attribute combination, with its count."""
    RECORDER.take()
    send()
    _, _, counts, _, _ = RECORDER.take()
    return [dict(key) | {"count": n} for key, n in counts.items()]


def test_it_is_on_unless_backlot_telemetry_or_do_not_track_says_off(
    hello, capsys, tmp_path, monkeypatch
):
    for on in (None, "0", ""):
        with _env(BACKLOT_TELEMETRY=None, DO_NOT_TRACK=on):
            get_settings.cache_clear()
            assert enabled(get_settings()), on
    # Read from `.env` as every setting is.
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".env").write_text("DO_NOT_TRACK=1\n", encoding="utf-8")
    with _env(BACKLOT_TELEMETRY=None, DO_NOT_TRACK=None):
        get_settings.cache_clear()
        assert not enabled(get_settings())
    monkeypatch.undo()
    with _env(BACKLOT_TELEMETRY="1", DO_NOT_TRACK=None), _ExportTo(UNREACHABLE):
        capsys.readouterr()
        with client_for(hello):
            pass
        # A server that sends says so, says why, and says how to stop it.
        assert capsys.readouterr().err.strip() == notice(UNREACHABLE)
    for off in ({"BACKLOT_TELEMETRY": "0"}, {"BACKLOT_TELEMETRY": "false"}, {"DO_NOT_TRACK": "1"}):
        with (
            _env(**{"BACKLOT_TELEMETRY": None, "DO_NOT_TRACK": None} | off),
            _ExportTo(UNREACHABLE),
        ):
            get_settings.cache_clear()
            assert not enabled(get_settings())
            with client_for(hello) as c:
                assert _calls(lambda: c.get("/github/repos/acme/docs-site", headers=ADMIN)) == []
            assert "usage counts" not in capsys.readouterr().err
    get_settings.cache_clear()


class _Started(Exception):
    pass


def test_each_way_to_start_a_server_can_turn_it_off(monkeypatch, hello, capsys):
    import uvicorn
    from typer.testing import CliRunner

    import backlot.cli
    import backlot.mcp

    seen = []

    def capture(*args, **kwargs):
        seen.append(
            enabled(get_settings())
            if "env" not in kwargs
            else kwargs["env"].get("BACKLOT_TELEMETRY")
        )
        raise _Started

    monkeypatch.setattr(uvicorn, "run", capture)
    monkeypatch.setattr(backlot.mcp, "run", capture)
    monkeypatch.setattr(backlot.server.subprocess, "Popen", capture)
    data_dir = str(hello.data_dir)
    # `--data-dir` and `--disable-telemetry` write the environment; both are put back afterwards.
    with _env(
        BACKLOT_TELEMETRY=None,
        DO_NOT_TRACK=None,
        BACKLOT_DATA_DIR=os.environ.get("BACKLOT_DATA_DIR"),
    ):
        runner = CliRunner()
        for argv in (
            ["serve", "--data-dir", data_dir],
            ["serve", "--disable-telemetry", "--data-dir", data_dir],
            ["mcp", "--data-dir", data_dir],
            ["mcp", "--disable-telemetry", "--data-dir", data_dir],
        ):
            os.environ.pop("BACKLOT_TELEMETRY", None)
            get_settings.cache_clear()
            assert isinstance(runner.invoke(backlot.cli.app, argv).exception, _Started)
        for telemetry in (True, False):
            os.environ.pop("BACKLOT_TELEMETRY", None)
            capsys.readouterr()
            with pytest.raises(_Started):
                with backlot.server.serve(data_dir=hello.data_dir, telemetry=telemetry):
                    pass
            # The child's own notice goes to its log, so serve() says it where its caller sees it.
            seen.append(notice(recorder.ENDPOINT) in capsys.readouterr().err)
    get_settings.cache_clear()
    assert seen == [True, False, True, False, None, True, "0", False]


def test_each_client_rule_names_the_headers_it_was_measured_from():
    assert [rule.label.partition("/")[0] for rule in clients._CLIENTS] == [
        "google-api-python-client",
        "google-auth",
        "boto3",
        "aiobotocore",
        "botocore",
        "slack_sdk",
        "hubspot-api-client",
        "notion-client",
        "PyGithub",
        "aiohttp",
        "python-httpx2",
        "kiota-python",
        "python-httpx",
        "python-requests",
        "python-urllib",
        "httplib2",
        "curl",
        "node",
    ]
    for rule in clients._CLIENTS:
        # The rule itself answers, not an earlier one, with the version the headers carry.
        label = clients.client(rule.sent)
        assert label.partition("/")[0] == rule.label.partition("/")[0], rule
        version = label.partition("/")[2]
        assert not version or re.search(rf"\b{re.escape(version)}\b", rule.sent[rule.header])
    assert clients.client({"user-agent": "Mozilla/5.0 (Macintosh) AppleWebKit/537.36"}) == "other"
    assert clients.client({}) == "other"


@pytest.mark.parametrize(
    "source, support, status, code, spent, expected",
    [
        ("github", "implemented", 403, "rate_limited", True, "rate_limit"),
        ("github", "implemented", 403, "unrecognized", False, "auth"),
        ("slack", "implemented", 429, "ratelimited", False, "rate_limit"),
        ("hubspot", "implemented", 401, "INVALID_AUTHENTICATION", False, "auth"),
        ("slack", "implemented", 200, "invalid_auth", False, "auth"),
        ("slack", "implemented", 200, "not_authed", False, "auth"),
        ("slack", "implemented", 200, "account_inactive", False, "auth"),
        # Slack's own code at 200, not an exception of Backlot's.
        ("slack", "implemented", 200, "internal_error", False, "client"),
        ("linear", "implemented", 200, "unsupported_filter", False, "unsupported"),
        ("gmail", "implemented", 501, "notImplemented", False, "client"),
        ("gmail", "implemented", 503, "backendError", False, "internal"),
    ],
)
def test_a_failure_is_classed_by_its_answer(source, support, status, code, spent, expected):
    assert error_class(source, support, status, code, spent) == expected
    assert error_class(source, support, status, code, spent, raised=True) == "internal"


@pytest.mark.parametrize(
    "source, status, body, expected",
    [
        ("slack", 200, b'{"ok":false,"error":"channel_not_found"}', "channel_not_found"),
        ("s3", 404, b"<Error><Code>NoSuchKey</Code></Error>", "NoSuchKey"),
        (
            "gmail",
            404,
            b'{"error":{"errors":[{"reason":"notFound"}],"status":"NOT_FOUND"}}',
            "notFound",
        ),
        ("google_drive", 404, b'{"error":{"code":404,"status":"NOT_FOUND"}}', "NOT_FOUND"),
        ("gmail", 400, b'{"error":"invalid_request"}', ""),
        ("notion", 404, b'{"object":"error","code":"object_not_found"}', "object_not_found"),
        (
            "hubspot",
            401,
            b'{"status":"error","category":"INVALID_AUTHENTICATION"}',
            "INVALID_AUTHENTICATION",
        ),
        ("github", 404, b'{"message":"Not Found"}', "not_found"),
        ("github", 422, b'{"message":"something Backlot never says"}', "unrecognized"),
        (
            "jira",
            404,
            b'{"errorMessages":["Issue does not exist or you do not have permission to see it."]}',
            "issue_not_found",
        ),
        (
            "confluence",
            404,
            b'{"message":"No content found with id: ContentId{id=1}"}',
            "content_not_found",
        ),
        (
            "fireflies",
            200,
            b'{"errors":[{"message":"x","extensions":{"code":"invalid_argument"}}]}',
            "invalid_argument",
        ),
        ("linear", 200, b'{"errors":[{"message":"Entity not found: Issue"}]}', "entity_not_found"),
        ("linear", 200, b'{"errors":[{"message":"x","extensions":[1]}]}', "unrecognized"),
        ("linear", 400, b'{"errors":[{"message":"x","extensions":[1]}]}', "graphql_validation"),
        ("linear", 200, b'{"errors":[{"message":"x","extensions":{"code":5}}]}', "unrecognized"),
        ("linear", 200, b'{"errors":[{"message":"x","extensions":{"code":""}}]}', "unrecognized"),
        ("fireflies", 200, b'{"data":{},"extensions":{"code":"X"}}', ""),
        ("fireflies", 400, b'{"data":{},"extensions":{"code":"X"}}', "X"),
        ("jira", 401, b"Client must be authenticated to access this resource.", "unauthenticated"),
        ("jira", 400, b"<!DOCTYPE html><html><head><title>(null)</title></head></html>", ""),
        ("msteams", 404, b'{"error":{"code":"NotFound","message":"x"}}', "NotFound"),
        ("msteams", 400, b'{"error":{"code":"","message":"Something else."}}', "unrecognized"),
        ("msteams", 400, b"<html><title>Bad Request</title></html>", ""),
    ],
)
def test_an_error_code_is_the_envelopes_own_identifier(source, status, body, expected):
    assert errors.error_code(source, status, body) == expected


GITHUB = "/github/repos/acme/docs-site"
TEAM = "/msgraph/v1.0/teams/778451ab-3734-4e45-beba-caf1bd343541"


@pytest.mark.parametrize(
    "method, path, kwargs, expected",
    [
        # (source, operation, support, status, error code, class)
        ("GET", GITHUB, {}, ("github", "repos/get", "implemented", 200, None, None)),
        ("HEAD", GITHUB, {}, ("github", "repos/get", "implemented", 200, None, None)),
        (
            "GET",
            "/github/repos/acme/nosuch",
            {},
            ("github", "repos/get", "implemented", 404, "not_found", "client"),
        ),
        (
            "GET",
            f"{GITHUB}/commits",
            {},
            ("github", "repos/list-commits", "missing", 307, None, None),
        ),
        (
            "POST",
            f"{GITHUB}/issues",
            {"json": {"title": "t"}},
            ("github", "issues/create", "missing", 405, None, "unsupported"),
        ),
        (
            "GET",
            f"{GITHUB}/actions/runs",
            {},
            (
                "github",
                "actions/list-workflow-runs-for-repo",
                "missing",
                404,
                "not_found",
                "unsupported",
            ),
        ),
        (
            "POST",
            "/slack/api/conversations.history",
            {"data": {"channel": "CNOPE"}},
            ("slack", "conversations.history", "implemented", 200, "channel_not_found", "client"),
        ),
        (
            "POST",
            "/slack/api/chat.scheduleMessage",
            {},
            ("slack", "chat.scheduleMessage", "missing", 404, None, "unsupported"),
        ),
        (
            "POST",
            "/slack/api/nonexistent.method",
            {},
            ("slack", "unmatched", "unknown", 200, "unknown_method", "client"),
        ),
        (
            "GET",
            "/atlassian/rest/api/3/project/ENG",
            {},
            ("jira", "getProject", "missing", 404, "no_endpoint", "unsupported"),
        ),
        (
            "GET",
            "/gmail/v1/users/me/messages",
            {},
            ("gmail", "gmail.users.messages.list", "implemented", 200, None, None),
        ),
        (
            "GET",
            "/gmail/v1/users/me/drafts",
            {},
            ("gmail", "gmail.users.drafts.list", "missing", 404, None, "unsupported"),
        ),
        # A route's `{param}` or `{rest:path}` does not take a literal of an operation it does
        # not serve: the vendor's router matches the literal.
        (
            "GET",
            f"{GITHUB}/issues/comments",
            {},
            ("github", "issues/list-comments-for-repo", "missing", 404, "not_found", "unsupported"),
        ),
        (
            "GET",
            f"{GITHUB}/branches/main/protection/required_signatures",
            {},
            (
                "github",
                "repos/get-commit-signature-protection",
                "missing",
                404,
                "branch_not_found",
                "unsupported",
            ),
        ),
        # x-amz-copy-source tells CopyObject from PutObject.
        (
            "PUT",
            "/s3/acme-artifacts/k2.txt",
            {"headers": {"x-amz-copy-source": "/acme-artifacts/k.txt"}},
            ("s3", "CopyObject", "refused", 400, "InvalidArgument", "unsupported"),
        ),
        # A read the vendor's published contract no longer describes has no vendor id; it is
        # named by its path template.
        (
            "GET",
            "/atlassian/wiki/rest/api/content/999999",
            {},
            ("confluence", "wiki/rest/api/content/{}", "extra", 404, "content_not_found", "client"),
        ),
        # A path a route serves under other methods is named by the route's template; one whose
        # template the collector would not keep (`values:batchGet`) stays unmatched.
        ("OPTIONS", GITHUB, {}, ("github", "repos/{}/{}", "unknown", 405, None, "client")),
        (
            "PUT",
            "/hubspot/crm/v3/objects/contacts/1",
            {},
            ("hubspot", "crm/v3/objects/{}/{}", "unknown", 405, None, "client"),
        ),
        (
            "PUT",
            f"{GITHUB}/contents",
            {},
            ("github", "repos/{}/{}/contents", "unknown", 405, None, "client"),
        ),
        (
            "POST",
            "/sheets/v4/spreadsheets/x/values:batchGet",
            {},
            ("google_drive", "unmatched", "unknown", 405, None, "client"),
        ),
        # The first route whose `{param}` takes the path is the matched one; a vendor template with
        # more literal segments, under any method, names the call.
        (
            "OPTIONS",
            "/hubspot/crm/v3/objects/contacts/search",
            {},
            ("hubspot", "crm/v3/objects/{}/search", "unknown", 401, None, "auth"),
        ),
        # An `Accept` refused on an operation a route serves is a call of that operation.
        (
            "POST",
            "/hubspot/crm/v3/objects/contacts/batch/read",
            {"headers": {"Accept": "text/html"}},
            (
                "hubspot",
                "post-/crm/v3/objects/{objectType}/batch/read_read",
                "implemented",
                406,
                None,
                "client",
            ),
        ),
        (
            "PUT",
            f"{GITHUB}/issues/comments",
            {},
            ("github", "repos/{}/{}/issues/comments", "unknown", 405, None, "client"),
        ),
        # Teams answers both from its catch-all or before routing: no route names them.
        (
            "OPTIONS",
            "/msgraph/v1.0/me",
            {},
            ("msteams", "unmatched", "unknown", 405, None, "client"),
        ),
        (
            "POST",
            "/msgraph/v1.0/me",
            {},
            ("msteams", "unmatched", "unknown", 400, "BadRequest", "client"),
        ),
        # Google Docs is mounted at /docs/v1, beside FastAPI's own /docs page.
        (
            "GET",
            "/docs/v1/documents/nosuch",
            {},
            ("google_drive", "docs.documents.get", "implemented", 404, "NOT_FOUND", "client"),
        ),
        (
            "HEAD",
            f"{GITHUB}/actions/runs",
            {},
            ("github", "actions/list-workflow-runs-for-repo", "missing", 404, None, "unsupported"),
        ),
        # A bearer token is no S3 credential; real answers it InvalidArgument, and so does Backlot.
        (
            "GET",
            "/s3/acme-artifacts?list-type=2",
            {},
            ("s3", "ListObjectsV2", "implemented", 400, "InvalidArgument", "client"),
        ),
        (
            "PUT",
            "/s3/acme-artifacts/k.txt",
            {"content": b"x"},
            ("s3", "PutObject", "refused", 400, "InvalidArgument", "unsupported"),
        ),
        (
            "GET",
            f"{TEAM}/Channels",
            {},
            ("msteams", "teams.ListChannels", "implemented", 200, None, None),
        ),
        (
            "GET",
            "/msgraph/v1.0/ME/chats",
            {},
            ("msteams", "me.ListChats", "missing", 404, "Request_ResourceNotFound", "unsupported"),
        ),
        (
            "POST",
            "/fireflies/graphql",
            {"headers": JSON, "content": b""},
            ("fireflies", "unparsed", "unknown", 400, "http", "client"),
        ),
        (
            "POST",
            "/fireflies/graphql",
            {"headers": JSON, "content": b"x" * (1024 * 1024 + 1)},
            ("fireflies", "unparsed", "unknown", 413, "http", "client"),
        ),
        (
            "POST",
            "/linear/graphql",
            {"headers": LINEAR | JSON, "content": b"x" * (10 * 1024 * 1024 + 1)},
            ("linear", "unparsed", "unknown", 413, "PAYLOAD_TOO_LARGE", "client"),
        ),
        (
            "POST",
            "/linear/graphql",
            {"headers": LINEAR | JSON, "content": b"{"},
            ("linear", "unparsed", "unknown", 400, None, "client"),
        ),
        (
            "GET",
            "/calendar/v3/calendars/primary/events",
            {},
            ("other", "unmatched", "unknown", 404, None, "client"),
        ),
    ],
)
def test_a_call_is_named_by_the_baseline(client, method, path, kwargs, expected):
    kwargs = dict(kwargs)
    headers = ADMIN | kwargs.pop("headers", {})
    calls = _calls(
        lambda: client.request(method, path, headers=headers, follow_redirects=False, **kwargs)
    )
    assert len(calls) == 1, calls
    c = calls[0]
    got = (
        c["backlot.source"],
        c["backlot.operation"],
        c["backlot.support"],
        c["http.response.status_code"],
        c.get("backlot.error_code"),
        c.get("backlot.error_class"),
    )
    assert got == expected
    assert c["http.request.method"] == method


@pytest.mark.parametrize(
    "query, encoding, expected",
    [
        (
            "{ issues(first: 1) { nodes { id } } }",
            None,
            ("query.issues", "implemented", 200, None, None),
        ),
        # Named by the document the route read, which Linear inflates.
        (
            "{ issues(first: 1) { nodes { id } } }",
            "gzip",
            ("query.issues", "implemented", 200, None, None),
        ),
        (
            "{ issues(first: 1) { nodes { id } } }",
            "deflate",
            ("query.issues", "implemented", 200, None, None),
        ),
        (
            '{ issue(id: "x") { id } teams { nodes { id } } }',
            None,
            ("query.issue,teams", "implemented", 200, "entity_not_found", "client"),
        ),
        (
            "{ projects { nodes { id } } }",
            None,
            ("query.projects", "missing", 400, "graphql_validation", "unsupported"),
        ),
        (
            "{ notAField }",
            None,
            ("query.unmatched", "unknown", 400, "graphql_validation", "client"),
        ),
        (
            'mutation { issueCreate(input: {title: "x", teamId: "t"}) { success } }',
            None,
            ("mutation", "unknown", 400, "graphql_validation", "client"),
        ),
        ("{ issues {", None, ("unparsed", "unknown", 400, "graphql_parse", "client")),
        (
            "{ __schema { queryType { name } } }",
            None,
            ("query.__schema", "implemented", 200, None, None),
        ),
    ],
)
def test_a_graphql_call_is_named_by_its_root_fields(client, query, encoding, expected):
    body = json.dumps({"query": query}).encode()
    headers = LINEAR | {"Content-Type": "application/json"}
    if encoding:
        body = gzip.compress(body) if encoding == "gzip" else zlib.compress(body)
        headers |= {"Content-Encoding": encoding}
    calls = _calls(lambda: client.post("/linear/graphql", headers=headers, content=body))
    assert len(calls) == 1, calls
    c = calls[0]
    assert (
        c["backlot.operation"],
        c["backlot.support"],
        c["http.response.status_code"],
        c.get("backlot.error_code"),
        c.get("backlot.error_class"),
    ) == expected
    assert (c["backlot.auth.kind"], c["backlot.caller.kind"]) == ("api_key", "admin")


def test_a_fragment_that_spreads_others_is_named_in_bounded_time():
    # Four fragments that each spread the next 80 times expand to 41 million visits.
    fragments = " ".join(
        f"fragment F{i} on Query {{ {' '.join([f'...F{i + 1}'] * 80)} }}" for i in range(4)
    )
    query = f"{{ ...F0 }} {fragments} fragment F4 on Query {{ issues {{ nodes {{ id }} }} }}"
    namer = naming.Namer()
    namer.source_of("/linear/graphql")  # loads the baselines outside the timing
    began = time.perf_counter()
    named = namer._graphql_call("linear", {"query": query})
    assert named.operation == "query.issues"
    assert time.perf_counter() - began < 1


J = "/atlassian/rest/api/3"
W = "/atlassian/wiki/rest/api"
ISSUE = "PLAD294FC-1825"
OLD_VERSION = {"X-GitHub-Api-Version": "1999-01-01"}


# One request per message-to-slug row and per alternative inside it, each sent to Backlot, so a
# router whose words drift from its row shows here. GitHub's rate-limit row is the spent-window
# test's; Linear's `unsupported_filter` is raised only for filter fields its schema does not
# declare, so no request reaches it.
@pytest.mark.parametrize(
    "method, path, kwargs, expected",
    [
        ("GET", "/github/repos/acme/nosuch", {}, "not_found"),
        ("GET", "/github/search/code?q=x", {"headers": {}}, "requires_authentication"),
        ("GET", GITHUB, {"headers": {"Authorization": "Bearer nope"}}, "bad_credentials"),
        ("GET", "/github/search/issues", {}, "validation_failed"),
        ("GET", "/github/search/issues?q=x&page=11&per_page=100", {}, "search_result_window"),
        ("GET", "/github/search/code?q=x&page=11&per_page=100", {}, "search_result_window"),
        ("GET", f"{GITHUB}/git/blobs/abc", {}, "invalid_sha"),
        ("GET", f"{GITHUB}/commits/nosuchref", {}, "no_commit_for_sha"),
        ("GET", f"{GITHUB}/contents/README.md?ref=nosuchref", {}, "no_commit_for_ref"),
        ("GET", f"{GITHUB}/branches/nosuch", {}, "branch_not_found"),
        ("GET", GITHUB, {"headers": OLD_VERSION}, "unsupported_api_version"),
        (
            "GET",
            f"{GITHUB}/contents/README.md",
            {"headers": OLD_VERSION},
            "unsupported_api_version",
        ),
        ("GET", f"{J}/issue/NOPE-1", {}, "issue_not_found"),
        ("GET", f"{J}/project/NOPE/role", {}, "project_not_found"),
        ("GET", f"{J}/project/12345/role", {}, "project_not_found"),
        ("GET", f"{J}/issue/{ISSUE}/comment?orderBy=bogus", {}, "invalid_order_by"),
        ("GET", f"{J}/project/search?orderBy=bogus", {}, "invalid_order_by"),
        ("GET", f"{J}/search/jql", {}, "unbounded_jql"),
        ("GET", f"{J}/search/jql?jql=x&maxResults=0", {}, "invalid_max_results"),
        ("GET", f"{J}/search/jql?jql=x&nextPageToken=garbage", {}, "invalid_next_page_token"),
        ("POST", f"{J}/search/jql", {"json": {"bogus": 1}}, "invalid_payload"),
        ("POST", f"{J}/search/jql", {"headers": JSON, "content": b""}, "invalid_payload"),
        ("POST", f"{J}/search/jql", {"headers": JSON, "content": b"{"}, "invalid_payload"),
        ("GET", f"{J}/issue/{ISSUE}/comment?maxResults=abc", {}, "invalid_parameter"),
        (
            "GET",
            f"{J}/search/jql?jql=x&" + "&".join(f"reconcileIssues={i}" for i in range(1, 52)),
            {},
            "invalid_parameter",
        ),
        ("GET", f"{J}/nopesuchroute", {}, "no_endpoint"),
        (
            "POST",
            f"{J}/search/jql",
            {"headers": {"Content-Type": "text/plain"}, "content": b"x"},
            "unsupported_media_type",
        ),
        ("GET", f"{J}/serverInfo", {"headers": {"Accept": "text/csv"}}, "not_acceptable"),
        ("DELETE", f"{J}/serverInfo", {}, "method_not_allowed"),
        ("OPTIONS", f"{J}/serverInfo", {"headers": {}}, "unauthenticated"),
        (
            "GET",
            f"{J}/serverInfo",
            {"headers": {"Authorization": "Bearer nope"}},
            "unauthenticated",
        ),
        ("GET", f"{W}/content/999999", {}, "content_not_found"),
        ("GET", f"{W}/content?spaceKey=NOPE", {}, "space_not_found"),
        ("GET", f"{W}/space/NOPE", {}, "space_not_found"),
        ("GET", f"{W}/search", {}, "cql_required"),
        (
            "GET",
            f"{W}/space",
            {"headers": {"Authorization": "Basic !!!notbase64"}},
            "unauthenticated",
        ),
        ("GET", f"{W}/space", {"headers": {}}, "forbidden"),
        ("GET", f"{W}/search?cql=type=page", {"headers": {}}, "forbidden"),
        ("GET", f"{W}/nopesuchroute", {"headers": {"Accept": "application/json"}}, "no_endpoint"),
        ("GET", f"{W}/space?limit=abc", {}, "invalid_parameter"),
        ("GET", f"{W}/space?limit=-1", {}, "invalid_parameter"),
        ("GET", f"{W}/content?type=bogus", {}, "content_type_not_found"),
        ("GET", f"{TEAM}/channels?$orderby=displayName", {}, "invalid_query_option"),
    ],
)
def test_each_message_slug_is_one_backlot_sends(client, method, path, kwargs, expected):
    kwargs = dict(kwargs)
    headers = kwargs.pop("headers", ADMIN)
    calls = _calls(lambda: client.request(method, path, headers=headers, **kwargs))
    assert [c.get("backlot.error_code") for c in calls] == [expected], calls


# Linear's rows; Fireflies runs the same engine, so a few of them are sent there as well. The
# `transcripts` rows there are Fireflies' own.
_GRAPHQL_MESSAGES = [
    ({"query": "{"}, LINEAR, "graphql_parse"),
    ({"query": '{ issue(id: "nope") { id } }'}, LINEAR, "entity_not_found"),
    ({"query": "{ __typename }"}, {}, "authentication_required"),
    ({"query": "{ nosuch }"}, LINEAR, "graphql_validation"),
    ({"query": '{ issue(id: "x", bogus: 1) { id } }'}, LINEAR, "graphql_validation"),
    ({"query": 'query($x: Nope) { issue(id: "x") { id } }'}, LINEAR, "graphql_validation"),
    ({"query": "{ issue { id } }"}, LINEAR, "graphql_validation"),
    ({"query": "query($x: String!) { issue(id: $x) { id } }"}, LINEAR, "graphql_validation"),
    (
        {
            "query": 'query($x: String = "a") { issue(id: $x) { id } }',
            "variables": {"x": None},
        },
        LINEAR,
        "graphql_validation",
    ),
    ({"query": "mutation { x }"}, LINEAR, "graphql_validation"),
    ({"query": "{ ...F }"}, LINEAR, "graphql_validation"),
    ({"query": "{ issues @nope { nodes { id } } }"}, LINEAR, "graphql_validation"),
    (
        {"query": "query A { viewer { id } } query A { viewer { id } }"},
        LINEAR,
        "graphql_validation",
    ),
    ({"query": "{ issue(id: 5) { id } }"}, LINEAR, "graphql_validation"),
    ({"query": "{ issue(id: null) { id } }"}, LINEAR, "graphql_validation"),
    ({"query": '{ issues(first: "x") { nodes { id } } }'}, LINEAR, "graphql_validation"),
    ({"query": "{ issues(orderBy: createdat) { nodes { id } } }"}, LINEAR, "graphql_validation"),
    ({"query": "fragment F on User { id } { viewer { id } }"}, LINEAR, "graphql_validation"),
    ({}, LINEAR, "graphql_request"),
    ([], LINEAR, "graphql_request"),
    ({"query": "{ viewer { id } }", "variables": "{"}, LINEAR, "graphql_request"),
    ({"query": "query A { viewer { id } }", "operationName": "B"}, LINEAR, "graphql_request"),
    (
        {"query": "query A { viewer { id } } query B { viewer { id } }"},
        LINEAR,
        "graphql_request",
    ),
]


@pytest.mark.parametrize(
    "path, payload, headers, expected",
    [("/linear/graphql", *row) for row in _GRAPHQL_MESSAGES]
    + [
        ("/fireflies/graphql", {"query": "{"}, ADMIN, "graphql_parse"),
        ("/fireflies/graphql", {"query": "{ nosuch }"}, ADMIN, "graphql_validation"),
        (
            "/fireflies/graphql",
            {"query": '{ transcripts(limit: "x") { id } }'},
            ADMIN,
            "graphql_validation",
        ),
        (
            "/fireflies/graphql",
            {"query": '{ transcripts(date: "x") { id } }'},
            ADMIN,
            "graphql_validation",
        ),
        ("/fireflies/graphql", {}, ADMIN, "graphql_request"),
    ],
)
def test_each_graphql_message_slug_is_one_backlot_sends(client, path, payload, headers, expected):
    calls = _calls(lambda: client.post(path, headers=headers, json=payload))
    assert [c.get("backlot.error_code") for c in calls] == [expected], calls


def test_every_message_slug_has_a_request_that_sends_it():
    sent = {
        row[-1]
        for t in (
            test_each_message_slug_is_one_backlot_sends,
            test_each_graphql_message_slug_is_one_backlot_sends,
        )
        for row in t.pytestmark[0].args[1]
    } | {"rate_limited", "unsupported_filter"}
    tables = errors._GITHUB + errors._JIRA + errors._CONFLUENCE + errors._GRAPHQL + errors._MSGRAPH
    assert {slug for _, slug in tables} <= sent


def test_how_a_call_authenticated_is_counted_without_its_credential(client, hello):
    tokens = __import__("yaml").safe_load(hello.tokens_path.read_text(encoding="utf-8"))
    user = tokens["users"][0]["token"]
    sigv4 = "AWS4-HMAC-SHA256 Credential=AKIAEXAMPLE/20261019/us-east-1/s3/aws4_request, SignedHeaders=host, Signature=00"
    basic = base64.b64encode(f"x:{user}".encode()).decode()
    cases = [
        ("GET", GITHUB, {}, ("anonymous", "anonymous")),
        ("GET", GITHUB, {"headers": ADMIN}, ("bearer", "admin")),
        ("GET", GITHUB, {"headers": {"Authorization": f"Bearer {user}"}}, ("bearer", "user")),
        ("GET", GITHUB, {"headers": {"Authorization": f"Basic {basic}"}}, ("basic", "user")),
        ("GET", GITHUB, {"headers": {"Authorization": sigv4}}, ("sigv4", "user")),
        ("GET", GITHUB, {"headers": {"Authorization": "AWS AKIAEXAMPLE:c2ln"}}, ("sigv2", "user")),
        (
            "GET",
            GITHUB,
            {"params": {"X-Amz-Credential": "AKIAEXAMPLE/20261019"}},
            ("presigned", "user"),
        ),
        ("GET", GITHUB, {"params": {"AWSAccessKeyId": "AKIAEXAMPLE"}}, ("presigned", "user")),
        ("GET", GITHUB, {"params": {"hapikey": user}}, ("api_key", "user")),
        ("POST", "/slack/api/auth.test", {"data": {"token": user}}, ("bearer", "user")),
        ("GET", "/slack/api/auth.test", {"params": {"token": user}}, ("bearer", "user")),
    ]
    for method, path, kwargs, expected in cases:
        (c,) = _calls(lambda: client.request(method, path, **kwargs))
        assert (c["backlot.auth.kind"], c["backlot.caller.kind"]) == expected, kwargs
        assert user not in json.dumps(c)
    # A credential with no `/` after it is read in linear time.
    began = time.perf_counter()
    _credential({"authorization": "AWS4-HMAC-SHA256 " + "Credential=" * 24_000}, {}, {})
    assert time.perf_counter() - began < 0.5


def test_a_spent_github_window_is_a_rate_limit(hello):
    with (
        _env(BACKLOT_TELEMETRY="1", DO_NOT_TRACK=None),
        _ExportTo(UNREACHABLE),
        client_for(hello) as c,
    ):
        for _ in range(100):
            if c.get("/github/search/issues?q=x", headers=ADMIN).status_code != 200:
                break
        (call,) = _calls(lambda: c.get("/github/search/issues?q=x", headers=ADMIN))
    assert (
        call["http.response.status_code"],
        call["backlot.error_code"],
        call["backlot.error_class"],
    ) == (403, "rate_limited", "rate_limit")


def test_a_403_with_a_window_left_is_auth(client):
    """The control for the spent window: a window left, and a `0` on another header."""
    from fastapi.responses import JSONResponse

    def forbidden():
        return JSONResponse(
            {"message": "Forbidden"},
            status_code=403,
            headers={"x-ratelimit-remaining": "12", "x-control": "0"},
        )

    client.app.add_api_route("/github/telemetry-forbidden", forbidden)
    try:
        (call,) = _calls(lambda: client.get("/github/telemetry-forbidden", headers=ADMIN))
    finally:
        client.app.router.routes.pop()
    assert (call["http.response.status_code"], call["backlot.error_class"]) == (403, "auth")


@pytest.mark.parametrize(
    "path, counted",
    [
        ("/health", 0),
        ("/_meta/users", 0),
        ("/openapi.json", 0),
        ("/docs", 0),
        # Google Docs is mounted beside FastAPI's own /docs page, and is counted.
        ("/docs/v1/documents/nosuch", 1),
    ],
)
def test_backlots_own_routes_are_not_counted(client, path, counted):
    assert len(_calls(lambda: client.get(path, headers=ADMIN))) == counted


def test_each_part_of_a_google_batch_is_a_call_of_its_own(client):
    boundary = "batch_boundary"
    parts = "".join(
        f"--{boundary}\r\nContent-Type: application/http\r\nContent-ID: <{i}>\r\n\r\n"
        f"GET /gmail/v1/users/me/{what} HTTP/1.1\r\n\r\n"
        for i, what in enumerate(["messages", "labels"])
    )
    body = f"{parts}--{boundary}--\r\n"
    headers = ADMIN | {
        "Content-Type": f"multipart/mixed; boundary={boundary}",
        "x-goog-api-client": "gl-python/3.13.0 gdcl/2.150.0",
    }
    calls = _calls(lambda: client.post("/batch/gmail/v1", headers=headers, content=body))
    # A part is re-sent by Backlot's own HTTP client, so it carries the batch's client label.
    assert sorted(
        (c["backlot.source"], c["backlot.operation"], c["backlot.client"]) for c in calls
    ) == [
        ("gmail", "batch", "google-api-python-client/2.150"),
        ("gmail", "gmail.users.labels.list", "google-api-python-client/2.150"),
        ("gmail", "gmail.users.messages.list", "google-api-python-client/2.150"),
    ]
    calls = _calls(lambda: client.post("/batch/calendar/v3", headers=headers, content=body))
    assert ("other", "unmatched") in [(c["backlot.source"], c["backlot.operation"]) for c in calls]
    assert "batch" not in [c["backlot.operation"] for c in calls]


def test_an_unhandled_exception_is_an_internal_error(client):
    def boom():
        raise RuntimeError("corpus-secret")

    client.app.add_api_route("/github/telemetry-boom", boom)
    try:
        with pytest.raises(RuntimeError):
            RECORDER.take()
            client.get("/github/telemetry-boom", headers=ADMIN)
        _, _, counts, _, _ = RECORDER.take()
    finally:
        client.app.router.routes.pop()
    (c,) = [dict(k) for k in counts]
    assert (
        c["http.response.status_code"],
        c["backlot.error_code"],
        c["backlot.error_class"],
        c["backlot.support"],
    ) == (500, "internal_error", "internal", "extra")
    assert "corpus-secret" not in json.dumps(c)


# A resolver made to raise by replacing the store read it makes, beside a refusal the same field
# raises on purpose.
@pytest.mark.parametrize(
    "path, headers, broken, query, expected",
    [
        ("/linear/graphql", LINEAR, "linear_by_id", '{ issue(id: "x") { id } }', "internal_error"),
        ("/linear/graphql", LINEAR, None, '{ issue(id: "nope") { id } }', "entity_not_found"),
        (
            "/fireflies/graphql",
            ADMIN,
            "list_fireflies_transcripts",
            "{ transcripts { id } }",
            "internal_error",
        ),
        (
            "/fireflies/graphql",
            ADMIN,
            None,
            "{ transcripts(limit: 0) { id } }",
            "invalid_arguments",
        ),
    ],
)
def test_an_exception_a_graphql_resolver_raises_is_an_internal_error(
    client, monkeypatch, path, headers, broken, query, expected
):
    def boom(*args, **kwargs):
        raise RuntimeError("corpus-secret")

    if broken:
        monkeypatch.setattr(store, broken, boom)
    (c,) = _calls(lambda: client.post(path, headers=headers, json={"query": query}))
    assert (
        c["http.response.status_code"],
        c["backlot.error_code"],
        c["backlot.error_class"],
    ) == (200, expected, "internal" if broken else "client")
    assert "corpus-secret" not in json.dumps(c)


def test_nothing_the_request_spelled_reaches_an_export(client):
    marker = "LEAK-7f3a9c"
    sent = [
        lambda: client.get(
            f"{GITHUB}/contents/{marker}.md?ref={marker}", headers=ADMIN | {"X-Note": marker}
        ),
        lambda: client.get(
            f"/github/repos/{marker}/{marker}/commits", headers=ADMIN, follow_redirects=False
        ),
        lambda: client.post("/slack/api/search.messages", headers=ADMIN, data={"query": marker}),
        # api.test answers with the `error` it is sent.
        lambda: client.post("/slack/api/api.test", headers=ADMIN, data={"error": marker}),
        lambda: client.post(f"/slack/api/api.test?error={marker}", headers=ADMIN),
        lambda: client.post(f"/slack/api/{marker}.method", headers=ADMIN),
        lambda: client.get(f"/atlassian/rest/api/3/issue/{marker}-1", headers=ADMIN),
        lambda: client.get(f"/{marker}/v1/{marker}", headers=ADMIN),
        lambda: client.post(
            "/linear/graphql",
            headers=LINEAR,
            json={
                "query": f'query {marker} {{ issue(id: "{marker}") {{ {marker}: id }} }}',
                "variables": {"x": marker},
            },
        ),
        lambda: client.post(
            "/linear/graphql", headers=LINEAR, json={"query": f"{{ {marker}Field }}"}
        ),
        lambda: client.get(f"/s3/acme-artifacts/{marker}?tagging", headers=ADMIN),
    ]
    RECORDER.take()
    for send in sent:
        send()
    body = encode(*RECORDER.take())
    text = json.dumps(body)
    assert marker not in text and marker.lower() not in text.lower()
    (metrics,) = body["resourceMetrics"][0]["scopeMetrics"]
    points = [
        p for m in metrics["metrics"] for p in (m.get("sum") or m.get("histogram"))["dataPoints"]
    ]
    assert len(points) >= len(sent)
    for point in points:
        for attr in point["attributes"]:
            pattern = STORED.get(attr["key"])
            value = attr["value"].get("stringValue")
            if pattern and value is not None:
                assert re.match(pattern, value), (attr["key"], value)
    keys = {a["key"] for a in body["resourceMetrics"][0]["resource"]["attributes"]}
    assert keys == {"service.name", "service.version", "os.type", "process.runtime.version"}


class _Collector(BaseHTTPRequestHandler):
    bodies: list = []

    def do_POST(self):
        self.bodies.append(json.loads(self.rfile.read(int(self.headers["Content-Length"]))))
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(b'{"partialSuccess":{}}')

    def log_message(self, *args):
        pass


@pytest.fixture
def collector():
    _Collector.bodies = []
    server = ThreadingHTTPServer(("127.0.0.1", 0), _Collector)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{server.server_port}/v1/metrics", _Collector.bodies
    server.shutdown()


def test_the_exporter_posts_each_interval_and_survives_a_dead_endpoint(collector, monkeypatch):
    url, bodies = collector
    recorder = Recorder()
    attrs = {
        "backlot.source": "github",
        "backlot.operation": "repos/get",
        "http.response.status_code": 200,
    }
    recorder.record(attrs, 3)
    recorder.record(attrs, 30)
    # How a call was answered splits its count, not its latency.
    recorder.record(attrs | {"http.response.status_code": 404, "backlot.error_code": "x"}, 3000)
    live = Exporter(recorder, url, 3600, surface=lambda: {("github", "implemented"): 32})
    live.flush()
    live.flush()  # an empty interval sends nothing
    assert (live.sent, live.failed, len(bodies)) == (1, 0, 1)
    (sums, hists, gauge) = bodies[0]["resourceMetrics"][0]["scopeMetrics"][0]["metrics"]
    assert gauge["name"] == "backlot.surface.operations"
    assert gauge["gauge"]["dataPoints"][0]["asInt"] == "32"
    assert sorted(p["asInt"] for p in sums["sum"]["dataPoints"]) == ["1", "2"]
    assert sums["sum"]["aggregationTemporality"] == 1
    assert len(hists["histogram"]["dataPoints"]) == 1
    assert hists["histogram"]["dataPoints"][0]["bucketCounts"] == [
        "1",
        "0",
        "0",
        "1",
        "0",
        "0",
        "0",
        "0",
        "0",
        "1",
        "0",
    ]

    recorder.record(attrs, 1)
    dead = Exporter(recorder, UNREACHABLE, 3600)
    dead.flush(timeout=1)
    assert (dead.sent, dead.failed) == (0, 1)

    # Once an interval, not only at shutdown.
    ticking = Exporter(recorder, url, 1)
    ticking.start()
    recorder.record(attrs, 1)
    deadline = time.monotonic() + 5
    while len(bodies) < 2 and time.monotonic() < deadline:
        time.sleep(0.05)
    # Before stop(), whose own flush would send the same call.
    assert len(bodies) == 2
    ticking.stop()

    for i in range(MAX_SERIES + 10):
        recorder.record({"backlot.operation": f"op{i}"}, 1)
    recorder.record({"backlot.operation": "op0"}, 1)  # a combination already held still counts
    interval = recorder.take()
    assert interval[2][(("backlot.operation", "op0"),)] == 2
    metrics = encode(*interval)["resourceMetrics"][0]["scopeMetrics"][0]["metrics"]
    (dropped,) = [m for m in metrics if m["name"] == "backlot.http.dropped"]
    assert dropped["sum"]["dataPoints"][0]["asInt"] == "10"

    # A resolver that stalls is outside httpx's timeout; stopping is still bounded.
    resolve = socket.getaddrinfo
    monkeypatch.setattr(
        socket,
        "getaddrinfo",
        lambda host, *a, **k: time.sleep(3) or resolve(host, *a, **k),
    )
    stalled = Exporter(recorder, "http://stalled.invalid/v1/metrics", 3600)
    recorder.record(attrs, 1)
    began = time.monotonic()
    stalled.stop(timeout=0.5)
    assert time.monotonic() - began < 1


def test_a_server_sends_what_it_counted_when_it_stops(hello, collector):
    url, bodies = collector
    with _env(BACKLOT_TELEMETRY="1", DO_NOT_TRACK=None), _ExportTo(url):
        with client_for(hello) as c:
            RECORDER.take()
            c.get(GITHUB, headers=ADMIN)
            c.get(f"{GITHUB}/commits", headers=ADMIN, follow_redirects=False)
    sent = [
        {a["key"]: a["value"] for a in p["attributes"]}
        for b in bodies
        for p in b["resourceMetrics"][0]["scopeMetrics"][0]["metrics"][0]["sum"]["dataPoints"]
    ]
    assert sorted(s["backlot.operation"]["stringValue"] for s in sent) == [
        "repos/get",
        "repos/list-commits",
    ]
    # The surface it serves goes along, counted from its own baselines.
    (gauge,) = [
        m
        for b in bodies
        for m in b["resourceMetrics"][0]["scopeMetrics"][0]["metrics"]
        if "gauge" in m
    ]
    points = {
        tuple(a["value"]["stringValue"] for a in p["attributes"]): int(p["asInt"])
        for p in gauge["gauge"]["dataPoints"]
    }
    assert points == {k: v for k, v in naming.surface().items()}
    assert {source for source, _ in points} == {
        "github",
        "slack",
        "gmail",
        "google_drive",
        "jira",
        "confluence",
        "notion",
        "s3",
        "hubspot",
        "linear",
        "fireflies",
        "msteams",
    }
