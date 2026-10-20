"""Real stdio/Streamable HTTP MCP clients and human-facing CLI."""

import argparse
import asyncio
import json
import os
import sys
from contextlib import asynccontextmanager
from urllib.parse import urlparse

import httpx
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from mcp.client.streamable_http import streamable_http_client

from .models import OpenAIModel
from .runtime import Agent, DemoModel, Trace, redact


@asynccontextmanager
async def connect(transport="stdio", url="http://127.0.0.1:8000/mcp"):
    if transport == "stdio":
        # Explicit copy preserves only operator-provided startup policy, never model data.
        params = StdioServerParameters(
            command=sys.executable,
            args=["-m", "iacpaas_mcp.server"],
            env=dict(os.environ),
            cwd=os.getcwd(),
        )
        async with stdio_client(params) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                yield session
    else:
        parsed = urlparse(url)
        if parsed.scheme != "https" and not (
            parsed.scheme == "http" and parsed.hostname in {"localhost", "127.0.0.1", "::1"}
        ):
            raise ValueError("HTTP transport requires HTTPS or loopback HTTP")
        token = os.environ.get("MCP_HTTP_TOKEN")
        if not token:
            raise ValueError("MCP_HTTP_TOKEN is required for HTTP")
        async with httpx.AsyncClient(
            headers={"Authorization": f"Bearer {token}"},
            timeout=httpx.Timeout(30, read=300),
            follow_redirects=False,
        ) as client:
            async with streamable_http_client(url, http_client=client) as (read, write, _):
                async with ClientSession(read, write) as session:
                    await session.initialize()
                    yield session


def human_confirm(name, arguments, *, non_interactive=False):
    if non_interactive or not sys.stdin.isatty():
        return False
    print(
        "MOCK write (volatile):",
        name,
        json.dumps(redact(arguments), ensure_ascii=False),
        file=sys.stderr,
    )
    try:
        return input("Подтвердить запись? Введите yes: ").strip() == "yes"
    except EOFError:
        return False


def parser():
    result = argparse.ArgumentParser(
        description="Native MCP agent; demo is deterministic, NOT an LLM"
    )
    result.add_argument("prompt")
    result.add_argument("--model", choices=["demo", "openai"], default="demo")
    result.add_argument("--transport", choices=["stdio", "http"], default="stdio")
    result.add_argument("--url", default="http://127.0.0.1:8000/mcp")
    result.add_argument("--trace")
    result.add_argument("--max-calls", type=int, default=12, choices=range(1, 13))
    result.add_argument("--non-interactive", action="store_true")
    return result


async def run(args):
    model = DemoModel() if args.model == "demo" else OpenAIModel.from_env()
    print(
        "Model: DETERMINISTIC SCENARIO PARSER (NOT LLM)"
        if args.model == "demo"
        else "Model: OpenAI-compatible endpoint (external request)"
    )
    trace = Trace(args.trace) if args.trace else None
    interactive = not args.non_interactive and sys.stdin.isatty()
    async with connect(args.transport, args.url) as session:
        agent = Agent(
            session,
            model,
            max_calls=args.max_calls,
            trace=trace,
            confirm=lambda name, arguments: human_confirm(
                name, arguments, non_interactive=not interactive
            ),
            clarify=(lambda question: input(question + " ")) if interactive else None,
        )
        return await agent.run(args.prompt)


def main(argv=None):
    args = parser().parse_args(argv)
    try:
        print(asyncio.run(run(args)))
        return 0
    except (Exception, KeyboardInterrupt) as exc:
        print("Agent error: " + str(redact(str(exc))), file=sys.stderr)
        return 1
