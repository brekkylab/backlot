"""ACL enforced end-to-end through real MCP servers pointed at Backlot.

Uses the ``live_server`` fixture (a real ``uvicorn`` on the conftest SAMPLE corpus) and drives a
real MCP server against it: a document the admin can read is blocked for an ACL-restricted user —
same tool, same object, different identity.

- **Atlassian** (`mcp-atlassian`, Docker) — skipped unless Docker is available.
- **Notion** (`@notionhq/notion-mcp-server`, npx) — skipped unless ``npx`` (Node) is on PATH; the
  first run downloads the npm package.
- **S3** (`awslabs.aws-api-mcp-server`, uvx) — skipped unless ``uvx`` is on PATH; the first run
  downloads the package. This one isn't an ACL test (it's a broad AWS-CLI wrapper, not read-one-
  object-at-a-time like the others): it just proves the server, pointed at Backlot via
  ``AWS_ENDPOINT_URL``, lists a bucket's objects through a real signed AWS CLI call.

The other seven sources reach MCP through Backlot's own bridge, ``backlot.mcp`` — the code behind
``backlot mcp`` — which is app code and so imported and driven in-process here, one source at a
time. The stdio tests at the end run the command itself.

All require the ``mcp`` package. The vendor-server params below intentionally **duplicate** the
wiring in the per-service example files (``examples/using-mcp-with-agents/{atlassian,notion,s3}.py``)
rather than importing them — a test must not reach into ``examples/`` (no ``sys.path`` hacks); a
little copied setup is the lesser evil.
"""

from __future__ import annotations

import asyncio
import base64
import collections
import dataclasses
import functools
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import time
import urllib.request

import pytest
import yaml

pytest.importorskip("mcp")

from backlot import mcp as backlot_mcp  # noqa: E402
from backlot import store, synth  # noqa: E402
from backlot.acl import Acl  # noqa: E402


def _docker_available() -> bool:
    if not shutil.which("docker"):
        return False
    try:
        return subprocess.run(["docker", "info"], capture_output=True, timeout=10).returncode == 0
    except Exception:  # noqa: BLE001
        return False


def _atlassian_params(base: str, token: str):
    """`docker run` args pointing mcp-atlassian at a local server (see examples/.../atlassian.py).

    mcp-atlassian only classifies a host as Atlassian *Cloud* when it ends in `.atlassian.net`, so
    use a fake `backlot.atlassian.net` mapped to the host via `--add-host`, and Basic auth where the
    api_token is a Backlot token (Backlot resolves it to a user and enforces that user's ACL)."""
    from mcp import StdioServerParameters

    port = base.rsplit(":", 1)[1]  # Docker reaches Backlot on the host via host-gateway
    host, url = "backlot.atlassian.net", f"http://backlot.atlassian.net:{port}"
    return StdioServerParameters(
        command="docker",
        args=[
            "run",
            "-i",
            "--rm",
            f"--add-host={host}:host-gateway",
            "-e",
            f"JIRA_URL={url}/atlassian",
            "-e",
            "JIRA_USERNAME=svc@example.com",
            "-e",
            f"JIRA_API_TOKEN={token}",
            "-e",
            f"CONFLUENCE_URL={url}/atlassian/wiki",
            "-e",
            "CONFLUENCE_USERNAME=svc@example.com",
            "-e",
            f"CONFLUENCE_API_TOKEN={token}",
            "-e",
            "MCP_ALLOWED_URL_DOMAINS=atlassian.net",
            "-e",
            "READ_ONLY_MODE=true",
            "ghcr.io/sooperset/mcp-atlassian:latest",
            "--transport",
            "stdio",
        ],
    )


def _notion_params(base: str, token: str):
    """`npx` args pointing the official notion-mcp-server at Backlot via BASE_URL."""
    from mcp import StdioServerParameters

    return StdioServerParameters(
        command="npx",
        args=["-y", "@notionhq/notion-mcp-server"],
        env={
            "BASE_URL": f"{base.rstrip('/')}/notion",
            "NOTION_TOKEN": token,
            "NOTION_VERSION": "2025-09-03",
        },
    )


def _restricted_doc(settings, user_token: str, source: str, where: str | None = None):
    """A doc of ``source`` the admin can read but this user cannot (per Backlot's own ACL)."""
    conn = store.connect_ro(settings.db_path)
    acl = Acl.load(settings.tokens_path, settings.admin_token, settings.org_name)
    caller = acl.resolve(user_token)
    vids = acl.visible_ids(conn, caller)
    for row in conn.execute(f"SELECT * FROM {store.table(source)} WHERE {where or '1=1'}"):
        key = tuple(row[c] for c in store.id_columns(source))
        if store.get_document(conn, source, *key, visible_ids=vids) is None:
            return row, caller.email
    return None, caller.email


async def _call(params, tool_pred, args, ok_pred) -> bool:
    """Connect via ``params``, call the tool matched by ``tool_pred`` with ``args``, and return
    ``ok_pred(text)`` over the response text."""
    from mcp import ClientSession
    from mcp.client.stdio import stdio_client

    async with stdio_client(params) as (r, w):
        async with ClientSession(r, w) as sess:
            await sess.initialize()
            tools = (await sess.list_tools()).tools
            tool = next(t for t in tools if tool_pred(t.name))
            res = await sess.call_tool(tool.name, args)
            text = "".join(getattr(c, "text", "") for c in res.content)
            return ok_pred(text)


# --------------------------------------------------------------------------- Atlassian


@pytest.mark.skipif(not _docker_available(), reason="Docker not available")
def test_mcp_atlassian_acl_enforced(live_server):
    base, settings = live_server
    user = yaml.safe_load(settings.tokens_path.read_text())["users"][0]
    row, email = _restricted_doc(settings, user["token"], "jira")
    assert row is not None, f"no Jira issue is ACL-restricted from {email} in the sample corpus"
    key = row["key"]  # the stored column: jira is probed, so a re-derived hash can disagree

    def reads(token):
        return asyncio.run(
            _call(
                _atlassian_params(base, token),
                tool_pred=lambda n: n == "jira_get_issue",
                args={"issue_key": key},
                ok_pred=lambda t: t.strip().startswith("{") and '"key"' in t,
            )
        )

    assert reads(settings.admin_token), "admin should read the issue through mcp-atlassian"
    assert not reads(user["token"]), f"{email} should be blocked from the issue via mcp-atlassian"


# --------------------------------------------------------------------------- Notion


@pytest.mark.skipif(shutil.which("npx") is None, reason="npx (Node) not available")
def test_mcp_notion_acl_enforced(live_server):
    base, settings = live_server
    user = yaml.safe_load(settings.tokens_path.read_text())["users"][0]
    row, email = _restricted_doc(settings, user["token"], "notion", "subtype IS NOT 'database'")
    assert row is not None, f"no Notion page is ACL-restricted from {email} in the sample corpus"
    # The row's own `id`, not a fresh `synth.notion_id(row["doc_id"])`: notion never
    # probes, so the two agree today, but a test that re-derives instead of reading the stored
    # column would stop exercising the ACL path the day that stops being true and not notice.
    page_id = row["id"]

    def reads(token):
        return asyncio.run(
            _call(
                _notion_params(base, token),
                # the proxy names tools from OpenAPI operationIds (e.g. "API-retrieve-a-page")
                tool_pred=lambda n: "retrieve-a-page" in n or ("page" in n and "retrieve" in n),
                args={"page_id": page_id},
                ok_pred=lambda t: '"object": "page"' in t or '"object":"page"' in t,
            )
        )

    assert reads(settings.admin_token), "admin should read the page through notion-mcp-server"
    assert not reads(user["token"]), (
        f"{email} should be blocked from the page via notion-mcp-server"
    )


# --------------------------------------------------------------------------- S3


def _s3_params(base: str, token: str):
    """`uvx` args pointing the awslabs aws-api MCP server at Backlot via AWS_ENDPOINT_URL (see
    examples/.../s3.py). The server shells the AWS CLI, whose boto3 client SigV4-signs each call;
    Backlot verifies the signature against the access-key/secret derived from ``token`` (the same
    pair GET /_meta/users exposes)."""
    from mcp import StdioServerParameters

    return StdioServerParameters(
        command="uvx",
        # `@latest`, so this keeps proving Backlot against the server awslabs ships today rather
        # than one we froze. The cost is that a version uvx has not got yet is downloaded here,
        # inside the window the client's `initialize` is waiting on, and that ends as
        # `McpError('Connection closed')` rather than as a slow pass. CI fetches the server in a
        # step above pytest so the download is never on this clock.
        # `--with mcp<2`: uvx resolves this server in an env of its own, and the server is still
        # on mcp v1 — it declares `mcp>=1.23.0` with no upper bound, yet imports
        # `mcp.shared.exceptions.McpError`, which v2 renamed to `MCPError`. What used to hold that
        # resolve on v1 was the server's own `fastmcp>=3.4.3`, whose fastmcp-slim capped `mcp<2`;
        # fastmcp 4.0 lifted the cap, so unconstrained it now takes mcp 2.x and dies on the
        # ImportError before speaking a byte of protocol. Our own env is on v2 (see the `mcp`
        # extra) — the two never share a process, only the wire protocol, which negotiates.
        args=["--with", "mcp<2", "awslabs.aws-api-mcp-server@latest"],
        env={
            "AWS_ENDPOINT_URL": f"{base.rstrip('/')}/s3",
            "AWS_ACCESS_KEY_ID": synth.s3_access_key_id(token),
            "AWS_SECRET_ACCESS_KEY": synth.s3_secret_access_key(token),
            "AWS_REGION": "us-east-1",
            "READ_OPERATIONS_ONLY": "true",
        },
    )


@pytest.mark.skipif(shutil.which("uvx") is None, reason="uvx not installed")
def test_mcp_s3_lists_objects(live_server):
    """The awslabs aws-api MCP server, pointed at Backlot, lists objects via a signed AWS CLI call."""
    base, settings = live_server
    params = _s3_params(base, settings.admin_token)
    out = asyncio.run(
        _call(
            params,
            # the server also exposes a "suggest_aws_commands" tool; pick the one that runs a command.
            tool_pred=lambda name: name == "call_aws",
            args={
                "cli_command": f"aws s3api list-objects-v2 --bucket eng-artifacts "
                f"--endpoint-url {base}/s3"
            },
            ok_pred=lambda text: "runbooks/oncall.md" in text,
        )
    )
    assert out, "expected the SAMPLE eng-artifacts/runbooks/oncall.md key in the listing"


# ------------------------------------------------------ the OpenAPI→MCP bridge (backlot.mcp)


def _bridge_call(base, source, creds, *, tool_pred, args, ok_pred) -> bool:
    """Exercise one source's bridge in-process: the same ``backlot.mcp.openapi_server`` that
    ``backlot mcp`` serves over stdio, driven through fastmcp's in-memory client. The spec it
    consumes is ``backlot.openapi``'s, unit-tested in ``tests/test_openapi.py``; what is proved
    here is the transport — the credential travels with every call, in whichever spelling the
    source authenticates with. Returns ``ok_pred`` over the tool's response text; a
    blocked/errored call is ``False``."""
    from fastmcp import Client

    async def _go():
        server = backlot_mcp.openapi_server(base, creds, source)
        async with Client(server) as c:
            tool = next(t for t in (await c.list_tools()) if tool_pred(t.name))
            res = await c.call_tool(tool.name, args)
            return ok_pred("".join(getattr(bl, "text", "") for bl in res.content))

    try:
        return asyncio.run(_go())
    except Exception:  # noqa: BLE001 — a blocked read may surface as a tool error
        return False


# --- one ACL property, seven surfaces -----------------------------------------------------
# Every generic-bridge source enforces the ACL the same way: the bridge forwards the caller's
# credential, so a document the admin reads through a tool is not-found for a scoped user. The
# cases differ only in which tool, which arguments, and how the document's id is spelled.
#
# Each `args` reads the row's OWN stored id column rather than re-deriving it from the dataset id.
# For github and hubspot that is load-bearing (both are probed, so a re-derived hash can disagree
# with what the row was assigned); for the rest it keeps the test exercising the ACL path on the
# day that stops being true.

_BRIDGE_CASES = [
    pytest.param(
        "github",
        "kind IS NULL OR kind != 'file'",  # a file has no served number; it is (repo, path)
        # `get_issue`, not `get_issue_comment` — a prefix match picks whichever the bridge lists
        # first, and these arguments only fit the one that takes a `number`
        lambda n: n.startswith("get_issue") and "comment" not in n,
        # the org the CORPUS produced, which tokens.yaml records — not `settings.org_name`, still
        # the unloaded default here. The GitHub surface 404s any other owner.
        lambda row, st: {
            "owner": yaml.safe_load(st.tokens_path.read_text())["org"],
            "repo": row["repo"],
            "number": row["number"],
        },
        lambda t, row: '"number"' in t and '"title"' in t,
        id="github",
    ),
    pytest.param(
        "hubspot",
        None,
        lambda n: n.startswith("get_object"),
        lambda row, st: {"object_type": row["object_type"], "record_id": row["id"]},
        lambda t, row: '"properties"' in t and row["id"] in t,
        id="hubspot",
    ),
    pytest.param(
        "slack",
        None,
        lambda n: n.startswith("search_messages"),
        lambda row, st: {"query": "reorg"},  # the restricted people-confidential message
        lambda t, row: "headcount" in t,  # a word only that message carries
        id="slack-search",
    ),
    pytest.param(
        "gmail",
        None,
        lambda n: n.startswith("gmail_messages_get"),
        lambda row, st: {"user_id": "me", "msg_id": row["id"], "format": "full"},
        lambda t, row: '"payload"' in t or '"snippet"' in t,
        id="gmail",
    ),
    pytest.param(
        "gdrive",
        None,
        lambda n: n.startswith("drive_files_get"),
        lambda row, st: {"file_id": row["id"]},
        lambda t, row: '"name"' in t and '"mimeType"' in t,
        id="google_drive",
    ),
    pytest.param(
        "notion",
        "subtype IS NOT 'database'",
        lambda n: n.startswith("get_page"),
        lambda row, st: {"page_id": row["id"]},
        lambda t, row: '"object": "page"' in t or '"object":"page"' in t,
        id="notion",
    ),
    pytest.param(
        "atlassian",
        None,
        lambda n: n.startswith("jira_get_issue"),
        lambda row, st: {"key": row["key"]},
        lambda t, row: '"key"' in t and '"fields"' in t,
        # nothing to pass: Basic for the resolved user, Bearer for the admin, both run below
        id="atlassian",
    ),
    pytest.param(
        "s3",
        None,
        lambda n: n.startswith("object_get"),
        lambda row, st: {"bucket": row["bucket"], "key": row["key"]},
        # the object's own body, which S3 returns verbatim rather than in an envelope
        lambda t, row: row["content"][:40] in t,
        id="s3",
    ),
]


@pytest.mark.parametrize("source, where, tool_pred, build_args, ok", _BRIDGE_CASES)
def test_mcp_bridge_enforces_the_acl(live_server, source, where, tool_pred, build_args, ok):
    """A document the admin reads through the bridge's own tool is not-found for a scoped user.

    The credentials come from ``backlot.mcp.resolve``, the same email-to-credentials step
    ``backlot mcp --user`` runs, so what is exercised is the resolution as well as the transport —
    including S3, whose calls are signed rather than bearer-authenticated."""
    pytest.importorskip("fastmcp")
    base, settings = live_server
    user = yaml.safe_load(settings.tokens_path.read_text())["users"][0]
    doc_source = {"gdrive": "google_drive", "atlassian": "jira"}.get(source, source)
    row, email = _restricted_doc(settings, user["token"], doc_source, where)
    assert row is not None, f"no {doc_source} document is ACL-restricted from {email} in SAMPLE"
    args = build_args(row, settings)

    def reads(creds):
        return _bridge_call(
            base, source, creds, tool_pred=tool_pred, args=args, ok_pred=lambda t: ok(t, row)
        )

    assert reads(backlot_mcp.resolve(base, None)), f"admin should read the {doc_source} document"
    assert not reads(backlot_mcp.resolve(base, email)), (
        f"{email} should be blocked from the {doc_source} document"
    )


def test_auth_header_spells_each_source_the_way_its_client_speaks():
    """Which scheme goes on the wire, asserted on the header rather than on an answer: Backlot's
    Atlassian surface accepts Bearer too (``auth.require_basic_or_bearer``), so no round-trip can
    tell the two apart. The reason the Basic branch exists is mcp-atlassian, which speaks
    ``email:api_token`` and nothing else, so that is what has to be pinned."""
    creds = backlot_mcp.Credentials(
        token="usr-abc", email="ava@acme.com", s3_access_key_id="AKIA", s3_secret_access_key="s"
    )
    header = backlot_mcp._auth_header(creds, "atlassian")["Authorization"]
    scheme, _, payload = header.partition(" ")
    assert scheme == "Basic"
    assert base64.b64decode(payload).decode() == "ava@acme.com:usr-abc"
    for source in ("slack", "github", "notion", "linear"):
        assert backlot_mcp._auth_header(creds, source) == {"Authorization": "Bearer usr-abc"}
    # the admin has no email to put in a username, so Atlassian falls back to Bearer for them
    admin = dataclasses.replace(creds, email=None)
    assert backlot_mcp._auth_header(admin, "atlassian") == {"Authorization": "Bearer usr-abc"}


def test_mcp_atlassian_bridge_authenticates_basic_for_the_resolved_user(live_server):
    """The Atlassian case above only asserts a scoped user is BLOCKED from a restricted issue, and
    `_bridge_call` reports a rejected credential the same way it reports a hidden document, so on
    its own it stays green with the Basic header corrupted. Two positive reads close that.

    The second one carries an email the corpus does not have, which is what pins the TOKEN: on
    this surface `Basic <known-email>:<anything>` authenticates on the username alone
    (``auth.resolve_basic``'s identity shortcut, measured — even an empty password answers 200), so
    a read as the resolved user cannot tell a working token from a mangled one. With the username
    unresolvable, only the password can have authenticated — which also catches the two being
    swapped, since a token in the username position has no ``@`` for the shortcut to fire on."""
    pytest.importorskip("fastmcp")
    base, settings = live_server
    email = yaml.safe_load(settings.tokens_path.read_text())["users"][0]["email"]
    creds = backlot_mcp.resolve(base, email)
    conn = store.connect_ro(settings.db_path)
    acl = Acl.load(settings.tokens_path, settings.admin_token, settings.org_name)
    vids = acl.visible_ids(conn, acl.resolve(creds.token))
    row = next(
        r
        for r in conn.execute("SELECT * FROM jira_issues")
        if store.get_document(conn, "jira", r["key"], visible_ids=vids) is not None
    )
    reads = functools.partial(
        _bridge_call,
        base,
        "atlassian",
        tool_pred=lambda n: n.startswith("jira_get_issue"),
        args={"key": row["key"]},
        ok_pred=lambda t: row["key"] in t and '"fields"' in t,
    )
    assert reads(creds), f"Basic {email}:<token> should read an issue that user can see"
    anonymous = dataclasses.replace(creds, email="nobody@example.invalid")
    assert reads(anonymous), "the token, not the username, has to be what authenticates"


def test_mcp_s3_bridge_signs_for_the_resolved_user(live_server):
    """The S3 case above asserts a scoped user CANNOT read a restricted object, and `_bridge_call`
    reports a refused signature the same way it reports a hidden object — so on its own that
    assertion would stay green if per-user key derivation broke. This is the other half: the same
    user's own keys sign a readable object successfully, so the refusal above is the ACL's."""
    pytest.importorskip("fastmcp")
    base, settings = live_server
    email = yaml.safe_load(settings.tokens_path.read_text())["users"][0]["email"]
    creds = backlot_mcp.resolve(base, email)
    conn = store.connect_ro(settings.db_path)
    acl = Acl.load(settings.tokens_path, settings.admin_token, settings.org_name)
    vids = acl.visible_ids(conn, acl.resolve(creds.token))
    row = next(
        r
        for r in conn.execute("SELECT * FROM s3_objects")
        if store.get_document(conn, "s3", r["bucket"], r["key"], visible_ids=vids) is not None
    )
    assert _bridge_call(
        base,
        "s3",
        creds,
        tool_pred=lambda n: n.startswith("object_get"),
        args={"bucket": row["bucket"], "key": row["key"]},
        ok_pred=lambda t: row["content"][:40] in t,
    ), f"{email}'s own access keys should sign a readable object"


# ------------------------------------------ Linear + Fireflies (GraphQL→MCP bridge)
# The other two sources are GraphQL-only, so there is no OpenAPI operation to slice and the
# bridge derives its tools from introspection instead (`backlot.graphql.mcp_tools`, unit-tested
# in tests/test_graphql.py, with the per-vendor documents checked in test_linear.py /
# test_fireflies.py). What is left to prove here is the same property the REST bridges prove:
# the tool carries the caller's credential, so Backlot's ACL decides what comes back.


def _graphql_bridge_call(base, source, creds, *, tool_name, args, ok_pred, depth) -> bool:
    """Exercise one GraphQL source's bridge in-process: ``backlot.mcp.graphql_server``, the same
    server ``backlot mcp`` runs over stdio, driven through fastmcp's in-memory client. The
    derivation it serves is unit-tested in ``tests/test_graphql.py``; what is proved here is that
    the tool posts its document with the caller's credential. Returns ``ok_pred`` over the tool's
    response text."""
    from fastmcp import Client

    async def _go():
        server = backlot_mcp.graphql_server(base, creds, source, depth=depth)
        async with Client(server) as c:
            res = await c.call_tool(tool_name, args)
            return ok_pred("".join(getattr(b, "text", "") for b in res.content))

    try:
        return asyncio.run(_go())
    except Exception:  # noqa: BLE001 — a blocked read may surface as a tool error
        return False


@pytest.mark.parametrize(
    "source, tool_name, depth",
    [
        # depth 1 for linear and the default 2 for fireflies, which is what the launchers pass.
        pytest.param("linear", "issue", 1, id="linear"),
        pytest.param("fireflies", "transcript", 2, id="fireflies"),
    ],
)
def test_mcp_graphql_bridge_enforces_the_acl(live_server, source, tool_name, depth):
    """A transcript or issue the admin reads through the derived tool is unreadable for a
    scoped user — a field error for Linear's non-null `Issue!`, a null for Fireflies."""
    pytest.importorskip("fastmcp")
    base, settings = live_server
    user = yaml.safe_load(settings.tokens_path.read_text())["users"][0]
    row, email = _restricted_doc(settings, user["token"], source)
    assert row is not None, f"no {source} document is ACL-restricted from {email} in SAMPLE"

    def reads(creds):
        return _graphql_bridge_call(
            base,
            source,
            creds,
            tool_name=tool_name,
            args={"id": row["id"]},
            ok_pred=lambda t: row["title"] in t,
            depth=depth,
        )

    assert reads(backlot_mcp.resolve(base, None)), f"admin should read the {source} document"
    assert not reads(backlot_mcp.resolve(base, email)), (
        f"{email} should be blocked from the {source} document"
    )


def test_mcp_graphql_bridge_round_trips_a_nested_filter(live_server):
    """The payoff of expanding input objects into the tool's JSON Schema: `IssueFilter` reaches
    the endpoint as a variable and narrows the result, and `pageInfo` comes back so the caller
    can tell whether more remains."""
    pytest.importorskip("fastmcp")
    base, settings = live_server
    row = next(
        iter(
            store.connect_ro(settings.db_path).execute(
                "SELECT title FROM linear_issues ORDER BY id LIMIT 1"
            )
        )
    )
    word = max(row["title"].split(), key=len)

    def ok(text):
        body = json.loads(text)["data"]["issues"]
        titles = [n["title"] for n in body["nodes"]]
        # every row matched, not merely some rows returned — an ignored filter would pass that
        return (
            bool(titles)
            and all(word.lower() in t.lower() for t in titles)
            and "hasNextPage" in body["pageInfo"]
        )

    assert _graphql_bridge_call(
        base,
        "linear",
        backlot_mcp.resolve(base, None),
        tool_name="issues",
        args={"filter": {"title": {"containsIgnoreCase": word}}, "first": 5},
        ok_pred=ok,
        depth=1,
    )


def test_mcp_hubspot_bridge_search_tool(live_server):
    """The polymorphic search operation reaches the bridge as a usable tool: filterGroups go through
    as a request body, and `total` comes back."""
    pytest.importorskip("fastmcp")
    base, settings = live_server
    assert _bridge_call(
        base,
        "hubspot",
        backlot_mcp.resolve(base, None),
        tool_pred=lambda n: n.startswith("search_objects"),
        args={
            "object_type": "companies",
            "filterGroups": [
                {"filters": [{"propertyName": "industry", "operator": "EQ", "value": "healthcare"}]}
            ],
        },
        ok_pred=lambda t: '"total"' in t and "Acme Health" in t,
    )


# ------------------------------------------------------------------ `backlot mcp` over stdio
# The command an MCP client runs. The in-memory tests above prove each source's bridge; these prove
# the process around it: the stdio transport, the per-source namespace, the server it starts when
# none answers, and that the person named on its command line is the one every tool call answers as.


def _mcp_params(*args, env=None):
    from mcp import StdioServerParameters

    # `-m backlot` through this interpreter, the way the examples and `backlot.server` run it.
    return StdioServerParameters(
        command=sys.executable, args=["-m", "backlot", "mcp", *args], env=env
    )


async def _tools_and_instructions(params, errlog=None) -> tuple[list[str], str | None]:
    """The tool names a session lists, and the ``instructions`` it advertised alongside them — a
    client feeds the latter to the model, so the two have to agree about which sources exist."""
    from mcp import ClientSession
    from mcp.client.stdio import stdio_client

    async with stdio_client(params, errlog=errlog or sys.stderr) as (r, w):
        async with ClientSession(r, w) as sess:
            init = await sess.initialize()
            names = [t.name for t in (await sess.list_tools()).tools]
            return names, init.instructions


async def _tool_names(params, errlog=None) -> list[str]:
    names, _ = await _tools_and_instructions(params, errlog=errlog)
    return names


def _served_tool_ids(base: str, source: str) -> set[str]:
    """The operationIds of one source's MCP-ready spec — by contract the tool names it yields."""
    with urllib.request.urlopen(f"{base}/_meta/openapi/{source}", timeout=10) as r:
        spec = json.load(r)
    return {
        op["operationId"]
        for item in spec["paths"].values()
        for method, op in item.items()
        if isinstance(op, dict) and "operationId" in op
    }


def test_backlot_mcp_serves_one_source_under_its_own_tool_names(live_server):
    """`--source slack` is the Slack bridge alone: every tool is one operation of the served spec,
    under the name the spec gives it, with no namespace in front."""
    base, _ = live_server
    names = asyncio.run(_tool_names(_mcp_params("--source", "slack", "--url", base)))
    assert set(names) == _served_tool_ids(base, "slack")
    assert "search_messages" in names


def test_backlot_mcp_serves_every_source_namespaced(live_server):
    """No `--source` is all of them in one server, each tool prefixed with its source so two
    vendors' same-named operations cannot collide — and none long enough for an MCP client's
    64-character cap to truncate."""
    base, _ = live_server
    names = asyncio.run(_tool_names(_mcp_params("--url", base)))
    by_source = collections.defaultdict(set)
    for name in names:
        source, _, tool = name.partition("_")
        by_source[source].add(tool)
    assert set(by_source) == set(backlot_mcp.SOURCES)
    for source in backlot_mcp.OPENAPI_SOURCES:
        assert by_source[source] == _served_tool_ids(base, source), source
    assert {"issue", "issues", "teams", "comments"} <= by_source["linear"]
    assert {"transcript", "transcripts", "user", "users"} <= by_source["fireflies"]
    assert {"list_buckets", "bucket_get", "object_get"} == by_source["s3"]
    assert max(map(len, names)) <= 64, sorted(names, key=len)[-3:]


def test_backlot_mcp_advertises_only_the_sources_it_mounted(live_server):
    """`instructions` is derived from what was mounted. A fixed list would tell the model that a
    `--source` run's missing sources are there to call, and it would read every tool-not-found as
    its own mistake."""
    base, _ = live_server
    names, instructions = asyncio.run(
        _tools_and_instructions(_mcp_params("--source", "slack", "--source", "s3", "--url", base))
    )
    assert {n.partition("_")[0] for n in names} == {"slack", "s3"}
    assert "slack_*" in instructions and "s3_*" in instructions
    for absent in ("gmail_*", "notion_*", "linear_*", "atlassian_*"):
        assert absent not in instructions, instructions
    # the note about Atlassian's two products is part of that list, so it goes too
    assert "Confluence" not in instructions, instructions


def test_backlot_mcp_scopes_every_tool_call_to_the_user(live_server):
    """Over the real command, `--user` is who each call answers as: the same search finds the
    restricted message for the admin (no --user) and not for the person named."""
    base, settings = live_server
    user = yaml.safe_load(settings.tokens_path.read_text())["users"][0]

    def finds_it(*who):
        return asyncio.run(
            _call(
                _mcp_params("--source", "slack", "--url", base, *who),
                tool_pred=lambda n: n == "search_messages",
                args={"query": "reorg"},  # the restricted people-confidential message
                ok_pred=lambda t: "headcount" in t,  # a word only that message carries
            )
        )

    assert finds_it()
    assert not finds_it("--user", user["email"])


def test_resolve_matches_an_email_case_insensitively(live_server):
    """Atlassian and Google both treat an address case-insensitively, so an email copied in the
    shape a Jira page displays it resolves to the same person — and carries the corpus's own
    spelling onward, which is what Atlassian's Basic header sends."""
    base, settings = live_server
    email = yaml.safe_load(settings.tokens_path.read_text())["users"][0]["email"]
    assert email == email.lower(), email
    for spelling in (email, email.upper(), email.capitalize()):
        assert backlot_mcp.resolve(base, spelling).email == email, spelling


def test_backlot_mcp_refuses_an_email_the_corpus_does_not_know(live_server):
    """A name that resolves to nobody is refused, not quietly promoted to the admin — whose
    unfiltered view is exactly what a caller naming a person did not ask for."""
    base, _ = live_server
    out = subprocess.run(
        [sys.executable, "-m", "backlot", "mcp", "--url", base, "--user", "nobody@example.com"],
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert out.returncode == 1, out
    assert "nobody@example.com" in out.stderr and "not one of the" in out.stderr, out.stderr


def test_backlot_mcp_starts_a_server_when_none_answers(tmp_path):
    """The one-line install: no `--url` and nothing on the default port, so the command starts a
    server itself — over the bundled corpus, the data dir being empty — says where on stderr, and
    takes it down when the client hangs up."""
    from mcp import ClientSession
    from mcp.client.stdio import stdio_client

    if backlot_mcp.is_backlot(backlot_mcp.DEFAULT_URL):
        # On a developer machine with `backlot serve` up, the command would attach to that server
        # instead — correct behaviour, but not the branch under test. CI never has the port taken.
        pytest.skip(
            f"a Backlot server is already on {backlot_mcp.DEFAULT_URL}; free it to run this"
        )

    errlog = tmp_path / "stderr.txt"
    params = _mcp_params(
        "--source", "slack", env={**os.environ, "BACKLOT_DATA_DIR": str(tmp_path / "data")}
    )

    async def _list_channels():
        with open(errlog, "w") as f:
            async with stdio_client(params, errlog=f) as (r, w):
                async with ClientSession(r, w) as sess:
                    await sess.initialize()
                    res = await sess.call_tool("conversations_list", {})
                    return "".join(getattr(c, "text", "") for c in res.content)

    body = json.loads(asyncio.run(_list_channels()))
    assert body["ok"] is True and body["channels"], body

    err = errlog.read_text()
    assert "bundled hello-world corpus" in err, err
    up = re.search(r"Backlot is up at http://127\.0\.0\.1:(\d+)", err)
    assert up, err
    port = int(up.group(1))
    # The started server goes with the session. uvicorn takes a moment to unwind, so poll rather
    # than assert once — but not for long: a server still answering after this is a leak.
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        try:
            socket.create_connection(("127.0.0.1", port), timeout=0.5).close()
        except OSError:
            break
        time.sleep(0.2)
    else:
        pytest.fail(f"the server backlot mcp started on port {port} is still listening")


def test_backlot_mcp_reports_an_unusable_corpus_and_exits(tmp_path):
    """When the data dir holds something that is not a corpus, the server the command starts dies
    before answering. What reaches the terminal is that server's own diagnosis — which file, and
    the `backlot import` that would fix it — as an exit, not a traceback through contextlib."""
    (tmp_path / "db.sqlite").write_text("not a database")
    out = subprocess.run(
        [sys.executable, "-m", "backlot", "mcp", "--source", "slack", "--data-dir", str(tmp_path)],
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert out.returncode == 1, out
    assert "no usable corpus" in out.stderr and "backlot import" in out.stderr, out.stderr
    assert "Traceback" not in out.stderr, out.stderr


def test_backlot_mcp_refuses_an_unreachable_url():
    """An explicit `--url` that does not answer is an error, not a fallback: the caller named a
    server, and quietly serving a different corpus in its place would be the wrong kind of help."""
    out = subprocess.run(
        [sys.executable, "-m", "backlot", "mcp", "--url", "http://127.0.0.1:1"],
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert out.returncode == 1, out
    assert "no Backlot server answers at http://127.0.0.1:1" in out.stderr
