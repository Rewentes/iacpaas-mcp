import pytest


@pytest.mark.asyncio
async def test_loop_discovers_and_links_real_tool_results():
    from standalone_agent.runtime import Agent, DemoModel

    class Session:
        def __init__(self):
            self.calls = []

        async def list_tools(self):
            from mcp.types import ListToolsResult, Tool, ToolAnnotations

            return ListToolsResult(
                tools=[
                    Tool(
                        name=n,
                        inputSchema={"type": "object"},
                        annotations=ToolAnnotations(readOnlyHint=n != "update_concept"),
                    )
                    for n in ["list_inforesources", "get_inforesource"]
                ]
            )

        async def call_tool(self, name, arguments):
            from mcp.types import CallToolResult

            self.calls.append((name, arguments))
            data = (
                {"resources": [{"path": "/actual/discovered"}]}
                if name == "list_inforesources"
                else {"name": "Discovered"}
            )
            return CallToolResult(
                content=[], structuredContent={"ok": True, "data": data, "backend": "mock"}
            )

    session = Session()
    result = await Agent(session, DemoModel()).run("найди университет и покажи метаданные")
    assert session.calls == [
        ("list_inforesources", {"filter": ""}),
        ("get_inforesource", {"path": "/actual/discovered"}),
    ]
    assert "Discovered" in result
    assert "mock" in result.lower()


@pytest.mark.asyncio
async def test_budget_counts_calls_in_batch_and_denies_untrusted_writes(tmp_path):
    from mcp.types import CallToolResult, ListToolsResult, Tool, ToolAnnotations

    from standalone_agent.runtime import Agent, Decision, ToolCall, Trace

    class Session:
        calls = []

        async def list_tools(self):
            return ListToolsResult(
                tools=[
                    Tool(
                        name=n,
                        inputSchema={"type": "object"},
                        annotations=ToolAnnotations(readOnlyHint=n != "update_concept"),
                    )
                    for n in ["update_concept", "count_graph"]
                ]
            )

        async def call_tool(self, name, arguments):
            self.calls.append(name)
            return CallToolResult(content=[], structuredContent={"ok": True})

    class Model:
        async def decide(self, prompt, tools, history):
            return Decision(
                calls=[
                    ToolCall("update_concept", {"token": "SECRET"}),
                    ToolCall("count_graph"),
                    ToolCall("count_graph"),
                ]
            )

    path = tmp_path / "trace.jsonl"
    session = Session()
    result = await Agent(session, Model(), max_calls=2, trace=Trace(path)).run("write")
    assert "budget" in result
    assert session.calls == ["count_graph"]
    import json

    records = [json.loads(line) for line in path.read_text().splitlines()]
    assert len(records) == 2
    assert records[0]["status"] == "denied"
    assert "SECRET" not in path.read_text()
    assert path.stat().st_mode & 0o777 == 0o600


@pytest.mark.asyncio
async def test_clarification_noninteractive_stops_without_tool():
    from mcp.types import ListToolsResult

    from standalone_agent.runtime import Agent, DemoModel

    class Session:
        async def list_tools(self):
            return ListToolsResult(tools=[])

    result = await Agent(Session(), DemoModel()).run("покажи подграф")
    assert "Уточните" in result


@pytest.mark.asyncio
async def test_failed_write_is_not_retried_even_if_model_requests_it():
    from mcp.types import CallToolResult, ListToolsResult, TextContent, Tool

    from standalone_agent.runtime import Agent, Decision, ToolCall

    class Session:
        calls = 0

        async def list_tools(self):
            return ListToolsResult(
                tools=[Tool(name="update_concept", inputSchema={"type": "object"})]
            )

        async def call_tool(self, name, arguments):
            self.calls += 1
            return CallToolResult(
                isError=True, content=[TextContent(type="text", text='{"code":"timeout"}')]
            )

    class Model:
        async def decide(self, prompt, tools, history):
            return Decision(calls=[ToolCall("update_concept", {"path": "/demo/university"})])

    session = Session()
    await Agent(session, Model(), max_calls=3, confirm=lambda n, a: True).run("write")
    assert session.calls == 1


def test_trace_redacts_key_and_bounds_file(tmp_path):
    import json

    from standalone_agent.runtime import Trace

    path = tmp_path / "bounded.jsonl"
    trace = Trace(path, max_bytes=1024, max_record_bytes=512)
    trace.record(name="count_graph", args={"key": "PRIVATE"}, result={}, status="ok", duration_ms=0)
    for _ in range(100):
        trace.record(
            name="count_graph", args={}, result={"huge": "x" * 10000}, status="ok", duration_ms=0
        )
    assert path.stat().st_size <= 1024
    assert "PRIVATE" not in path.read_text()
    assert all(json.loads(line) for line in path.read_text().splitlines())
