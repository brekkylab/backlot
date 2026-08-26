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
import json
import shutil
import subprocess

import pytest
import yaml

pytest.importorskip("mcp")

from backlot import store, synth  # noqa: E402
from backlot.acl import Acl  # noqa: E402
from backlot.graphql import mcp_tools  # noqa: E402


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


def _restricted_doc(settings, user_token: str, source: str, where: str | None = None):
    """A doc of ``source`` the admin can read but this user cannot (per the mock's own ACL)."""
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
    """`uvx` args pointing the awslabs aws-api MCP server at the mock via AWS_ENDPOINT_URL (see
    examples/.../s3.py). The server shells the AWS CLI, whose boto3 client SigV4-signs each call;
    the mock verifies the signature against the access-key/secret derived from ``token`` (the same
    pair GET /_mock/users exposes)."""
    from mcp import StdioServerParameters

    return StdioServerParameters(
        command="uvx",
        # `@latest`, so this keeps proving the mock against the server awslabs ships today rather
        # than one we froze. The cost is that a version uvx has not got yet is downloaded here,
        # inside the window the client's `initialize` is waiting on, which ends as
        # `McpError('Connection closed')` — 49.6s to fail, against 2.3s once uvx has it. CI fetches
        # the server in a step above pytest so that download is never on this clock.
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
        None,
        id="github",
    ),
    pytest.param(
        "hubspot",
        None,
        lambda n: n.startswith("get_object"),
        lambda row, st: {"object_type": row["object_type"], "record_id": row["id"]},
        lambda t, row: '"properties"' in t and row["id"] in t,
        None,
        id="hubspot",
    ),
    pytest.param(
        "slack",
        None,
        lambda n: n.startswith("search_messages"),
        lambda row, st: {"query": "reorg"},  # the restricted people-confidential message
        lambda t, row: "headcount" in t,  # a word only that message carries
        None,
        id="slack-search",
    ),
    pytest.param(
        "gmail",
        None,
        lambda n: n.startswith("gmail_messages_get"),
        lambda row, st: {"user_id": "me", "msg_id": row["id"], "format": "full"},
        lambda t, row: '"payload"' in t or '"snippet"' in t,
        None,
        id="gmail",
    ),
    pytest.param(
        "gdrive",
        None,
        lambda n: n.startswith("drive_files_get"),
        lambda row, st: {"file_id": row["id"]},
        lambda t, row: '"name"' in t and '"mimeType"' in t,
        None,
        id="google_drive",
    ),
    pytest.param(
        "notion",
        "subtype IS NOT 'database'",
        lambda n: n.startswith("get_page"),
        lambda row, st: {"page_id": row["id"]},
        lambda t, row: '"object": "page"' in t or '"object":"page"' in t,
        None,
        id="notion",
    ),
    pytest.param(
        "atlassian",
        None,
        lambda n: n.startswith("jira_get_issue"),
        lambda row, st: {"key": row["key"]},
        lambda t, row: '"key"' in t and '"fields"' in t,
        "svc@example.com",  # this bridge authenticates with basic auth
        id="atlassian",
    ),
]


@pytest.mark.parametrize("source, where, tool_pred, build_args, ok, username", _BRIDGE_CASES)
def test_mcp_bridge_enforces_the_acl(
    live_server, source, where, tool_pred, build_args, ok, username
):
    """A document the admin reads through the bridge's own tool is not-found for a scoped user."""
    pytest.importorskip("fastmcp")
    base, settings = live_server
    user = yaml.safe_load(settings.tokens_path.read_text())["users"][0]
    doc_source = {"gdrive": "google_drive", "atlassian": "jira"}.get(source, source)
    row, email = _restricted_doc(settings, user["token"], doc_source, where)
    assert row is not None, f"no {doc_source} document is ACL-restricted from {email} in SAMPLE"
    args = build_args(row, settings)

    def reads(token):
        return _bridge_call(
            base,
            source,
            token,
            username=username,
            tool_pred=tool_pred,
            args=args,
            ok_pred=lambda t: ok(t, row),
        )

    assert reads(settings.admin_token), f"admin should read the {doc_source} document"
    assert not reads(user["token"]), f"{email} should be blocked from the {doc_source} document"


# ------------------------------------------ Linear + Fireflies (GraphQL→MCP bridge)
# The other two sources are GraphQL-only, so there is no OpenAPI operation to slice and the
# bridge derives its tools from introspection instead (`backlot.graphql.mcp_tools`, unit-tested
# in tests/test_graphql.py, with the per-vendor documents checked in test_linear.py /
# test_fireflies.py). What is left to prove here is the same property the REST bridges prove:
# the tool carries the caller's credential, so the mock's ACL decides what comes back.


def _graphql_bridge_call(base, source, token, *, tool_name, args, ok_pred, depth) -> bool:
    """Exercise the GraphQL→MCP bridge path WITHOUT touching ``examples/``.

    Introspects the endpoint, derives its tools, and serves the one under test through an
    in-memory FastMCP client. Like ``_bridge_call`` above, this **duplicates** the example
    bridge's transport wiring rather than importing it; the logic worth testing is the
    derivation, which lives in the app. Returns ``ok_pred`` over the tool's response text."""
    import httpx
    from fastmcp import Client, FastMCP
    from fastmcp.tools import Tool
    from fastmcp.tools.tool import ToolResult

    endpoint = f"{base}/{source}/graphql"
    headers = {"Authorization": f"Bearer {token}"}
    intro = httpx.post(
        endpoint, json={"query": mcp_tools.INTROSPECTION_QUERY}, headers=headers, timeout=30
    ).json()
    spec = next(t for t in mcp_tools.derive_tools(intro, depth=depth) if t.name == tool_name)

    class _Passthrough(Tool):
        document: str

        async def run(self, arguments):
            async with httpx.AsyncClient(timeout=30) as client:
                r = await client.post(
                    endpoint,
                    json={
                        "query": self.document,
                        "variables": mcp_tools.with_page_default(spec, arguments),
                    },
                    headers=headers,
                )
            body = r.json()
            failed = bool(body.get("errors")) and body.get("data") is None
            return ToolResult(content=json.dumps(body), is_error=failed)

    server = FastMCP()
    server.add_tool(
        _Passthrough(
            name=spec.name,
            description=spec.description,
            parameters=spec.input_schema,
            document=spec.document,
        )
    )

    async def _go():
        async with Client(server) as c:
            res = await c.call_tool(spec.name, args)
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

    def reads(token):
        return _graphql_bridge_call(
            base,
            source,
            token,
            tool_name=tool_name,
            args={"id": row["id"]},
            ok_pred=lambda t: row["title"] in t,
            depth=depth,
        )

    assert reads(settings.admin_token), f"admin should read the {source} document"
    assert not reads(user["token"]), f"{email} should be blocked from the {source} document"


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
        settings.admin_token,
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
