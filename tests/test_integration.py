"""Acceptance checks across the actual stdio MCP protocol boundary."""

import os
import subprocess
import sys
from pathlib import Path

import pytest
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.asyncio
async def test_linked_tools_over_stdio():
    """The second real tool call consumes the path returned by the first."""
    params = StdioServerParameters(
        command=sys.executable,
        args=["-m", "iacpaas_mcp.server"],
        cwd=str(ROOT),
        env={
            **os.environ,
            "PYTHONPATH": str(ROOT / "src"),
            "IACPAAS_BACKEND": "mock",
            "IACPAAS_ALLOW_WRITES": "0",
        },
    )
    async with stdio_client(params) as (reader, writer):
        async with ClientSession(reader, writer) as client:
            await client.initialize()
            catalog = await client.list_tools()
            tools = {tool.name: tool for tool in catalog.tools}
            assert "get_inforesource" in tools
            assert tools["get_inforesource"].outputSchema
            found = await client.call_tool("list_inforesources", {"filter": "университет"})
            assert not found.isError, found
            found_data = found.structuredContent
            assert found_data["ok"] is True
            assert found_data["backend"] == "mock"
            resource = found_data["data"]["resources"][0]
            metadata = await client.call_tool("get_inforesource", {"path": resource["path"]})
            assert not metadata.isError, metadata
            assert metadata.structuredContent["data"]["path"] == resource["path"]


def test_requested_real_backend_never_silently_uses_mock():
    result = subprocess.run(
        [sys.executable, "-m", "iacpaas_mcp.server"],
        env={**os.environ, "IACPAAS_BACKEND": "iacpaas"},
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert result.returncode != 0, "A requested real backend must fail, not run mock"
    assert "unsupported_backend" in result.stderr
