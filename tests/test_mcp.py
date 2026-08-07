"""ACL enforced end-to-end through real MCP servers pointed at the mock.

Uses the ``live_server`` fixture (a real ``uvicorn`` on the conftest SAMPLE corpus) and drives a
real MCP server against it: a document the admin can read is blocked for an ACL-restricted user —
same tool, same object, different identity.

- **Atlassian** (`mcp-atlassian`, Docker) — skipped unless Docker is available.
- **Notion** (`@notionhq/notion-mcp-server`, npx) — skipped unless ``npx`` (Node) is on PATH; the
  first run downloads the npm package.
- **S3** (`awslabs.aws-api-mcp-server`, uvx) — skipped unless ``uvx`` is on PATH; the first run
  downloads the package. This one isn't an ACL test (it's a broad AWS-CLI wrapper, not read-one-
  object-at-a-time like the others): it just proves the server, pointed at the mock via
  ``AWS_ENDPOINT_URL``, lists a bucket's objects through a real signed AWS CLI call.

All require the ``mcp`` package. The stdio params below intentionally **duplicate** the wiring in
the per-service example files (``examples/using-mcp-with-agents/{atlassian,notion,s3}.py``) rather
than importing them — a test must not reach into ``examples/`` (no ``sys.path`` hacks); a little
copied setup is the lesser evil.
"""

from __future__ import annotations

import asyncio
import shutil
import subprocess

import pytest
import yaml

pytest.importorskip("mcp")

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
    """`docker run` args pointing mcp-atlassian at a local mock (see examples/.../atlassian.py).

    mcp-atlassian only classifies a host as Atlassian *Cloud* when it ends in `.atlassian.net`, so
    use a fake `mock.atlassian.net` mapped to the host via `--add-host`, and Basic auth where the
    api_token is a mock token (the mock resolves it to a user and enforces that user's ACL)."""
    from mcp import StdioServerParameters

    port = base.rsplit(":", 1)[1]  # Docker reaches the host mock via host-gateway
    host, url = "mock.atlassian.net", f"http://mock.atlassian.net:{port}"
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
    """`npx` args pointing the official notion-mcp-server at the mock via BASE_URL."""
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


def _restricted_doc(settings, user_token: str, source: str, where: str = "1=1"):
    """A doc of ``source`` the admin can read but this user cannot (per the mock's own ACL)."""
    conn = store.connect_ro(settings.db_path)
    acl = Acl.load(settings.tokens_path, settings.admin_token, settings.org_name)
    caller = acl.resolve(user_token)
    vids = acl.visible_ids(conn, caller)
    for row in conn.execute(f"SELECT * FROM {store.table(source)} WHERE {where}"):
        if store.get_document(conn, source, row["doc_id"], visible_ids=vids) is None:
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
    key = synth.jira_key(row["doc_id"], synth.jira_project_key(row["project"]))

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
    page_id = synth.notion_id(row["doc_id"])

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
    """`uvx` args pointing the awslabs aws-api MCP server at the mock via AWS_ENDPOINT_URL (see
    examples/.../s3.py). The server shells the AWS CLI, whose boto3 client SigV4-signs each call;
    the mock verifies the signature against the access-key/secret derived from ``token`` (the same
    pair GET /_mock/users exposes)."""
    from mcp import StdioServerParameters

    return StdioServerParameters(
        command="uvx",
        args=["awslabs.aws-api-mcp-server@latest"],
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
    """The awslabs aws-api MCP server, pointed at the mock, lists objects via a signed AWS CLI call."""
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


# ------------------------------------------------------ GitHub (generic OpenAPI→MCP bridge)


def _bridge_call(base, source, token, *, tool_pred, args, ok_pred, username=None) -> bool:
    """Exercise the OpenAPI→MCP bridge path WITHOUT touching ``examples/``.

    Fetches the mock's MCP-ready spec (``GET /_mock/openapi/<source>`` — produced by ``backlot.openapi``,
    which owns the slice/dedupe logic) and serves it via an in-memory FastMCP client over an auth'd
    httpx client. That is the whole of what the example bridge does; the meaningful logic lives in
    the app and is unit-tested in ``tests/test_openapi.py``. Returns ``ok_pred`` over the tool's
    response text; a blocked/errored call is ``False``."""
    import base64 as b64

    import httpx
    from fastmcp import Client, FastMCP

    spec = httpx.get(f"{base}/_mock/openapi/{source}", timeout=10).json()
    if username:  # Atlassian: Basic username:token (the api_token IS the mock token)
        header = {
            "Authorization": "Basic " + b64.b64encode(f"{username}:{token}".encode()).decode()
        }
    else:
        header = {"Authorization": f"Bearer {token}"}

    async def _go():
        client = httpx.AsyncClient(base_url=base, headers=header, timeout=30)
        server = FastMCP.from_openapi(openapi_spec=spec, client=client, validate_output=False)
        async with Client(server) as c:
            tool = next(t for t in (await c.list_tools()) if tool_pred(t.name))
            res = await c.call_tool(tool.name, args)
            return ok_pred("".join(getattr(bl, "text", "") for bl in res.content))

    try:
        return asyncio.run(_go())
    except Exception:  # noqa: BLE001 — a blocked read may surface as a tool error
        return False


def test_mcp_github_bridge_acl_enforced(live_server):
    """A GitHub issue the admin can read via the bridge's get_issue tool is 404 for a scoped user."""
    pytest.importorskip("fastmcp")
    base, settings = live_server
    user = yaml.safe_load(settings.tokens_path.read_text())["users"][0]
    row, email = _restricted_doc(settings, user["token"], "github")
    assert row is not None, f"no GitHub issue is ACL-restricted from {email} in the sample corpus"
    number = synth.github_number(row["doc_id"])
    owner, repo = settings.org_name, row["repo"]

    def reads(token):
        return _bridge_call(
            base,
            "github",
            token,
            tool_pred=lambda n: n.startswith("get_issue"),
            args={"owner": owner, "repo": repo, "number": number},
            ok_pred=lambda t: '"number"' in t and '"title"' in t,
        )

    assert reads(settings.admin_token), "admin should read the issue through the OpenAPI bridge"
    assert not reads(user["token"]), f"{email} should be blocked from the issue via the bridge"


# ------------------------------------------------------ Slack (generic OpenAPI→MCP bridge)


def test_mcp_hubspot_bridge_acl_enforced(live_server):
    """A CRM record the admin can read through the bridge's get_object tool is 404 for a scoped user.

    HubSpot has no base-URL-switchable vendor MCP server, so the OpenAPI bridge is its only MCP
    path — and because the API is polymorphic over `{object_type}`, one tool covers every object
    type rather than there being a tool per type."""
    pytest.importorskip("fastmcp")
    base, settings = live_server
    user = yaml.safe_load(settings.tokens_path.read_text())["users"][0]
    row, email = _restricted_doc(settings, user["token"], "hubspot")
    assert row is not None, f"no HubSpot record is ACL-restricted from {email} in the sample corpus"
    record_id = synth.hubspot_record_id(row["doc_id"])

    def reads(token):
        return _bridge_call(
            base,
            "hubspot",
            token,
            tool_pred=lambda n: n.startswith("get_object"),
            args={"object_type": row["object_type"], "record_id": record_id},
            ok_pred=lambda t: '"properties"' in t and record_id in t,
        )

    assert reads(settings.admin_token), "admin should read the record through the OpenAPI bridge"
    assert not reads(user["token"]), f"{email} should be blocked from the record via the bridge"


def test_mcp_hubspot_bridge_search_tool(live_server):
    """The polymorphic search operation reaches the bridge as a usable tool: filterGroups go through
    as a request body, and `total` comes back."""
    pytest.importorskip("fastmcp")
    base, settings = live_server
    assert _bridge_call(
        base,
        "hubspot",
        settings.admin_token,
        tool_pred=lambda n: n.startswith("search_objects"),
        args={
            "object_type": "companies",
            "filterGroups": [
                {"filters": [{"propertyName": "industry", "operator": "EQ", "value": "healthcare"}]}
            ],
        },
        ok_pred=lambda t: '"total"' in t and "Acme Health" in t,
    )


def test_mcp_slack_bridge_acl_enforced(live_server):
    """A message in an ACL-restricted Slack channel is found by admin search but not a scoped user."""
    pytest.importorskip("fastmcp")
    base, settings = live_server
    user = yaml.safe_load(settings.tokens_path.read_text())["users"][0]
    row, email = _restricted_doc(settings, user["token"], "slack")
    assert row is not None, f"no Slack message is ACL-restricted from {email} in the sample corpus"

    def finds(token):
        return _bridge_call(
            base,
            "slack",
            token,
            tool_pred=lambda n: n.startswith("search_messages"),
            args={"query": "reorg"},  # the restricted people-confidential message
            ok_pred=lambda t: "headcount" in t,
        )  # a word only that message carries

    assert finds(settings.admin_token), "admin search should surface the restricted message"
    assert not finds(user["token"]), f"{email} search should not surface the restricted message"


# ------------------------------------------------------ Gmail (generic OpenAPI→MCP bridge)


def test_mcp_gmail_bridge_acl_enforced(live_server):
    """A Gmail message the admin can read via the bridge's messages.get tool is 404 for a user."""
    pytest.importorskip("fastmcp")
    base, settings = live_server
    user = yaml.safe_load(settings.tokens_path.read_text())["users"][0]
    row, email = _restricted_doc(settings, user["token"], "gmail")
    assert row is not None, f"no Gmail message is ACL-restricted from {email} in the sample corpus"
    # Gmail serves hex ids, not dsids (#39) — the bridge forwards whatever the caller passes, so a
    # dsid here would be refused as an invalid id value before the ACL was ever consulted.
    from backlot import synth

    msg_id = synth.gmail_message_id(row["doc_id"])

    def reads(token):
        return _bridge_call(
            base,
            "gmail",
            token,
            tool_pred=lambda n: n.startswith("gmail_messages_get"),
            args={"user_id": "me", "msg_id": msg_id, "format": "full"},
            ok_pred=lambda t: '"payload"' in t or '"snippet"' in t,
        )

    assert reads(settings.admin_token), "admin should read the message through the bridge"
    assert not reads(user["token"]), f"{email} should be blocked from the message via the bridge"


# ------------------------------------------------------ Google Drive (generic OpenAPI→MCP bridge)


def test_mcp_gdrive_bridge_acl_enforced(live_server):
    """A Drive file the admin can read via the bridge's files.get tool is 404 for a scoped user."""
    pytest.importorskip("fastmcp")
    base, settings = live_server
    user = yaml.safe_load(settings.tokens_path.read_text())["users"][0]
    row, email = _restricted_doc(settings, user["token"], "google_drive")
    assert row is not None, f"no Drive file is ACL-restricted from {email} in the sample corpus"
    file_id = row["doc_id"]

    def reads(token):
        return _bridge_call(
            base,
            "gdrive",
            token,
            tool_pred=lambda n: n.startswith("drive_files_get"),
            args={"file_id": file_id},
            ok_pred=lambda t: '"name"' in t and '"mimeType"' in t,
        )

    assert reads(settings.admin_token), "admin should read the file through the bridge"
    assert not reads(user["token"]), f"{email} should be blocked from the file via the bridge"


# ------------------------------------------------------ Notion (generic OpenAPI→MCP bridge)
# Notion also has a vendor-server example (test_mcp_notion_acl_enforced); this proves the same
# ACL additively through the generic bridge, with no vendor server.


def test_mcp_notion_bridge_acl_enforced(live_server):
    pytest.importorskip("fastmcp")
    base, settings = live_server
    user = yaml.safe_load(settings.tokens_path.read_text())["users"][0]
    row, email = _restricted_doc(settings, user["token"], "notion", "subtype IS NOT 'database'")
    assert row is not None, f"no Notion page is ACL-restricted from {email} in the sample corpus"
    page_id = synth.notion_id(row["doc_id"])

    def reads(token):
        return _bridge_call(
            base,
            "notion",
            token,
            tool_pred=lambda n: n.startswith("get_page"),
            args={"page_id": page_id},
            ok_pred=lambda t: '"object": "page"' in t or '"object":"page"' in t,
        )

    assert reads(settings.admin_token), "admin should read the page through the bridge"
    assert not reads(user["token"]), f"{email} should be blocked from the page via the bridge"


# ------------------------------------------------------ Atlassian (generic OpenAPI→MCP bridge)
# Atlassian also has a vendor-server example (test_mcp_atlassian_acl_enforced, Docker); this
# proves the same ACL additively through the generic bridge (basic auth), with no vendor server.


def test_mcp_atlassian_bridge_acl_enforced(live_server):
    pytest.importorskip("fastmcp")
    base, settings = live_server
    user = yaml.safe_load(settings.tokens_path.read_text())["users"][0]
    row, email = _restricted_doc(settings, user["token"], "jira")
    assert row is not None, f"no Jira issue is ACL-restricted from {email} in the sample corpus"
    key = synth.jira_key(row["doc_id"], synth.jira_project_key(row["project"]))

    def reads(token):
        return _bridge_call(
            base,
            "atlassian",
            token,
            username="svc@example.com",
            tool_pred=lambda n: n.startswith("jira_get_issue"),
            args={"key": key},
            ok_pred=lambda t: '"key"' in t and '"fields"' in t,
        )

    assert reads(settings.admin_token), (
        "admin should read the issue through the bridge (basic auth)"
    )
    assert not reads(user["token"]), f"{email} should be blocked from the issue via the bridge"
