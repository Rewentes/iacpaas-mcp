import json

import httpx
import pytest


@pytest.mark.asyncio
async def test_openai_sends_discovered_schema_and_linked_tool_messages():
    from mcp.types import Tool

    from standalone_agent.models import OpenAIModel

    requests = []

    def respond(request):
        requests.append(json.loads(request.content))
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {
                            "role": "assistant",
                            "content": None,
                            "tool_calls": [
                                {
                                    "id": "call_1",
                                    "type": "function",
                                    "function": {
                                        "name": "count_graph",
                                        "arguments": '{"path":"/demo/university"}',
                                    },
                                }
                            ],
                        }
                    }
                ]
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        model = OpenAIModel("https://example.invalid/v1", "test-key", "test-model", client=client)
        tools = [Tool(name="count_graph", inputSchema={"type": "object"})]
        first = await model.decide("count", tools, [])
        assert first.calls[0].id == "call_1"
        await model.decide(
            "count",
            tools,
            [
                {
                    "id": "call_1",
                    "name": "count_graph",
                    "arguments": {"path": "/demo/university"},
                    "result": {"ok": True},
                    "is_error": False,
                }
            ],
        )
    assert requests[0]["tools"][0]["function"]["name"] == "count_graph"
    assert requests[1]["messages"][-1]["tool_call_id"] == "call_1"


def test_cli_help_and_noninteractive_denial():
    from standalone_agent.cli import human_confirm, parser

    args = parser().parse_args(["write", "--model", "demo", "--non-interactive"])
    assert args.model == "demo"
    assert not human_confirm("update_concept", {"confirmed": True}, non_interactive=True)


@pytest.mark.asyncio
async def test_real_stdio_discovery_and_linked_calls(tmp_path):
    from standalone_agent.cli import connect
    from standalone_agent.runtime import Agent, DemoModel, Trace

    async with connect() as session:
        result = await Agent(session, DemoModel(), trace=Trace(tmp_path / "stdio.jsonl")).run(
            "найди университет и покажи метаданные"
        )
    records = [json.loads(line) for line in (tmp_path / "stdio.jsonl").read_text().splitlines()]
    assert [r["name"] for r in records] == ["list_inforesources", "get_inforesource"]
    assert records[1]["args"]["path"] == records[0]["result"]["data"]["resources"][0]["path"]
    assert "mock" in result.lower()
    assert all(record["status"] == "ok" for record in records)


def test_vscode_configs_and_custom_agent_are_coherent():
    from pathlib import Path

    root = Path(__file__).resolve().parents[1]
    config = json.loads((root / ".vscode/mcp.json").read_text())
    assert config["servers"]["iacpaas"]["args"] == ["run", "--frozen", "iacpaas-mcp"]
    assert config["servers"]["iacpaas"]["cwd"] == "${workspaceFolder}"
    config = json.loads((root / ".vscode/mcp.http.example.json").read_text())
    assert config["inputs"][0]["password"] is True
    assert config["servers"]["iacpaas"]["headers"]["Authorization"].startswith(
        "Bearer \u0024{input:"
    )
    assert "tools: ['iacpaas/*']" in (root / ".github/agents/iacpaas.agent.md").read_text()


@pytest.mark.asyncio
async def test_real_http_discovery_and_count(monkeypatch, tmp_path):
    import asyncio
    import os
    import socket
    import subprocess
    import sys

    from standalone_agent.cli import connect
    from standalone_agent.runtime import Agent, DemoModel, Trace

    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    monkeypatch.setenv("MCP_HTTP_TOKEN", "synthetic-test-local-token")
    process = subprocess.Popen(
        [sys.executable, "-m", "iacpaas_mcp.server", "--transport", "http", "--port", str(port)],
        env=dict(os.environ),
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        for _ in range(100):
            try:
                reader, writer = await asyncio.open_connection("127.0.0.1", port)
                writer.close()
                await writer.wait_closed()
                break
            except OSError:
                if process.poll() is not None:
                    pytest.fail("HTTP MCP subprocess exited before startup")
                await asyncio.sleep(0.05)
        else:
            pytest.fail("HTTP MCP startup timeout")
        async with connect("http", f"http://127.0.0.1:{port}/mcp") as session:
            result = await Agent(session, DemoModel(), trace=Trace(tmp_path / "http.jsonl")).run(
                "посчитай граф /demo/university"
            )
        events = [json.loads(line) for line in (tmp_path / "http.jsonl").read_text().splitlines()]
        assert [e["name"] for e in events] == ["list_inforesources", "count_graph"]
        assert events[-1]["result"]["data"]["vertices"] == 5
        assert events[-1]["result"]["data"]["edges"] == 6
        assert all(e["status"] == "ok" for e in events)
        assert "mock" in result.lower()
    finally:
        process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)


@pytest.mark.asyncio
async def test_real_stdio_confirmed_write_readback(monkeypatch, tmp_path):
    from standalone_agent.cli import connect, human_confirm
    from standalone_agent.runtime import Agent, DemoModel, Trace, decode_result

    monkeypatch.setenv("IACPAAS_ALLOW_WRITES", "1")
    monkeypatch.setenv("IACPAAS_WRITABLE_PREFIX", "/demo/")
    monkeypatch.setattr("sys.stdin.isatty", lambda: True)
    confirmations = []

    def answer(question):
        confirmations.append(question)
        return "yes"

    monkeypatch.setattr("builtins.input", answer)
    async with connect() as session:
        await Agent(
            session, DemoModel(), confirm=human_confirm, trace=Trace(tmp_path / "approved.jsonl")
        ).run("измени комментарий /demo/university")
        raw = await session.call_tool(
            "get_subgraph", {"path": "/demo/university", "root": "root", "depth": 0}
        )
        root = decode_result(raw)["data"]["nodes"][0]
        assert root["comment"] == "Демонстрационная запись mock"
    assert len(confirmations) == 1
    assert "yes" in confirmations[0]


@pytest.mark.asyncio
async def test_real_stdio_wrong_path_recovery(tmp_path):
    from standalone_agent.cli import connect
    from standalone_agent.runtime import Agent, DemoModel, Trace

    path = tmp_path / "recovery.jsonl"
    async with connect() as session:
        result = await Agent(session, DemoModel(), trace=Trace(path)).run(
            "покажи метаданные /missing/resource"
        )
    records = [json.loads(line) for line in path.read_text().splitlines()]
    assert len(records) == 3
    assert records[1]["result"]["code"] == "not_found"
    assert records[1]["status"] == "error"
    assert records[-1]["args"]["path"] == "/demo/university"
    assert records[-1]["status"] == "ok"
    assert "Университет" in result
