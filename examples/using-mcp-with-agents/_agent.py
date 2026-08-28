"""Run an LLM agent against a live MCP server. Shared by the per-service examples.

Each service example (`atlassian.py`, `notion.py`, `s3.py`) builds its own
``StdioServerParameters`` (pointing a real MCP server at Backlot) and calls
``run_agent(agent, params, question)``. Two agent backends ship:

- ``anthropic`` (default) — Claude via the Anthropic SDK's beta MCP tool runner. Needs
  ``ANTHROPIC_API_KEY`` and ``pip install -e ".[mcp]"``.
- ``openai`` — the OpenAI Agents SDK. Needs ``OPENAI_API_KEY`` and the ``agents`` package.

The SDKs are imported lazily inside each runner, so you only need the one you use.
"""

from __future__ import annotations

import asyncio
import os
import sys

INSTRUCTIONS = (
    "You answer questions about the company using its knowledge base, reached through the "
    "provided MCP tools. Make a few targeted searches, read the most relevant documents, then "
    "answer. Be decisive and efficient:\n"
    "- Don't repeat near-identical searches or scan every space/project/bucket; two or three "
    "searches are plenty.\n"
    "- Don't reach for unrelated tools or services.\n"
    "- The exact thing asked about may not exist in this corpus. If a couple of searches don't "
    "surface a clear match, STOP searching and answer from the closest relevant documents, "
    "briefly noting what wasn't found — do not keep hunting.\n"
    "Only use information returned by the tools; cite the titles."
)


async def _run_anthropic(params, question: str) -> None:
    from anthropic import AsyncAnthropic
    from anthropic.lib.tools.mcp import async_mcp_tool
    from mcp import ClientSession
    from mcp.client.stdio import stdio_client

    client = AsyncAnthropic()
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as mcp_client:
            await mcp_client.initialize()
            tools = await mcp_client.list_tools()
            runner = client.beta.messages.tool_runner(
                model=os.environ.get("ANTHROPIC_MODEL", "claude-sonnet-4-6"),
                max_tokens=16000,
                thinking={"type": "adaptive"},
                system=INSTRUCTIONS,
                messages=[{"role": "user", "content": question}],
                tools=[async_mcp_tool(t, mcp_client) for t in tools.tools],
            )
            async for message in runner:
                for block in message.content:
                    if block.type == "text":
                        print(block.text, end="", flush=True)
            print()


async def _run_openai(params, question: str) -> None:
    from agents import Agent, Runner
    from agents.mcp import MCPServerStdio

    async with MCPServerStdio(
        name="mcp",
        params={"command": params.command, "args": params.args, "env": params.env},
        client_session_timeout_seconds=30,
        cache_tools_list=True,
    ) as server:
        agent = Agent(
            name="Enterprise RAG agent",
            instructions=INSTRUCTIONS,
            mcp_servers=[server],
            model=os.environ.get("OPENAI_MODEL", "gpt-5.5"),
        )
        result = await Runner.run(agent, question, max_turns=int(os.environ.get("MAX_TURNS", "20")))
        print(result.final_output)


_API_KEYS = {"anthropic": "ANTHROPIC_API_KEY", "openai": "OPENAI_API_KEY"}


def run_agent(agent: str | None, params, question: str) -> None:
    """Run ``question`` against the MCP server described by ``params`` using ``agent``
    (``anthropic`` — the default — or ``openai``)."""
    runners = {"anthropic": _run_anthropic, "openai": _run_openai}
    choice = (agent or "anthropic").lower()
    if choice not in runners:
        sys.exit(f"--agent must be one of {sorted(runners)}, got {agent!r}")
    # Fail early with a clear message rather than a cryptic SDK auth error mid-run.
    if not os.environ.get(_API_KEYS[choice]):
        other = "openai" if choice == "anthropic" else "anthropic"
        sys.exit(
            f"{_API_KEYS[choice]} is not set — the --agent {choice} run needs it. "
            f"Export {_API_KEYS[choice]}=…, or use --agent {other} (needs {_API_KEYS[other]})."
        )
    asyncio.run(runners[choice](params, question))
