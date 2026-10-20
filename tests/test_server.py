import importlib.util

import pytest
from mcp.shared.memory import create_connected_server_and_client_session


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.mark.anyio
async def test_catalog_real_mcp_session_has_success_schema():
    assert importlib.util.find_spec("iacpaas_mcp.server") is not None
    from iacpaas_mcp.server import create_server

    async with create_connected_server_and_client_session(create_server()) as session:
        tools = {t.name: t for t in (await session.list_tools()).tools}
        tool = tools["list_inforesources"]
        assert tool.outputSchema["properties"]["ok"]["const"] is True
        assert tool.annotations.readOnlyHint is True
        result = await session.call_tool("list_inforesources", {})
        assert not result.isError
        assert result.structuredContent["ok"] is True
        assert result.structuredContent["backend"] == "mock"
        assert isinstance(result.structuredContent["warnings"], list)


@pytest.mark.anyio
async def test_read_surface_and_errors_are_structured():
    import json

    from iacpaas_mcp.server import create_server

    async with create_connected_server_and_client_session(create_server()) as session:
        for name, args in [
            ("get_inforesource", {"path": "/demo/university"}),
            ("get_subgraph", {"path": "/demo/university", "depth": 1}),
            ("count_graph", {"path": "/demo/university"}),
            ("query_graph", {"path": "/demo/university", "value": ""}),
            ("export_inforesource", {"path": "/demo/university"}),
        ]:
            result = await session.call_tool(name, args)
            assert not result.isError, result
            assert result.structuredContent["ok"] is True
        result = await session.call_tool("get_inforesource", {"path": "/missing"})
        assert result.isError
        assert json.loads(result.content[0].text)["code"] == "not_found"
        result = await session.call_tool("get_subgraph", {"path": "/demo/university", "depth": 6})
        assert result.isError
        assert json.loads(result.content[0].text)["code"] == "invalid_data"


@pytest.mark.anyio
async def test_write_gate_direct_calls_and_opt_in_are_server_side(monkeypatch, caplog):
    import json

    from iacpaas_mcp.server import create_server

    monkeypatch.delenv("IACPAAS_ALLOW_WRITES", raising=False)
    server = create_server()
    denied = await server.call_tool(
        "update_concept", {"path": "/demo/university", "concept_id": "root", "comment": "changed"}
    )
    assert denied.isError
    assert json.loads(denied.content[0].text)["code"] == "permission_denied"
    monkeypatch.setenv("IACPAAS_ALLOW_WRITES", "1")
    monkeypatch.setenv("IACPAAS_WRITABLE_PREFIX", "/demo/")
    # Policy is captured at trusted startup, not re-read for each tool invocation.
    assert (
        await server.call_tool("update_concept", {"path": "/demo/university", "concept_id": "root"})
    ).isError
    with caplog.at_level("INFO"):
        async with create_connected_server_and_client_session(create_server()) as session:
            tools = {t.name: t for t in (await session.list_tools()).tools}
            assert tools["update_concept"].annotations.readOnlyHint is False
            assert tools["import_inforesource"].annotations.destructiveHint is True
            result = await session.call_tool(
                "update_concept",
                {"path": "/demo/university", "concept_id": "root", "comment": "changed"},
            )
            assert not result.isError, result
            assert any("volatile mock" in w for w in result.structuredContent["warnings"])
            exported = await session.call_tool("export_inforesource", {"path": "/demo/university"})
            root = next(n for n in exported.structuredContent["data"]["nodes"] if n["id"] == "root")
            assert root["comment"] == "changed"
            denied = await session.call_tool(
                "import_inforesource", {"payload": {"path": "/private/x"}}
            )
            assert json.loads(denied.content[0].text)["code"] == "permission_denied"
    assert "mock_write" in caplog.text
    assert "changed" not in caplog.text


@pytest.mark.anyio
async def test_validation_and_byte_budgets_precede_unbounded_work():
    import json

    from iacpaas_mcp.server import create_server

    server = create_server(max_result_bytes=256)
    result = await server.call_tool("export_inforesource", {"path": "/demo/university"})
    assert result.isError
    assert json.loads(result.content[0].text)["code"] == "limit_exceeded"
    async with create_connected_server_and_client_session(create_server()) as session:
        exported = await session.call_tool("export_inforesource", {"path": "/demo/university"})
        result = await session.call_tool(
            "validate_resource", {"payload": exported.structuredContent["data"]}
        )
        assert not result.isError
        assert result.structuredContent["data"]["valid"] is True

    class NeverCalled:
        def get_subgraph(self, **kwargs):
            pytest.fail("invalid depth reached service")

    result = await create_server(NeverCalled()).call_tool(
        "get_subgraph", {"path": "x", "depth": 99}
    )
    assert json.loads(result.content[0].text)["code"] == "invalid_data"
    result = await create_server(max_argument_bytes=64).call_tool(
        "validate_resource", {"payload": {"x": "a" * 100}}
    )
    assert json.loads(result.content[0].text)["code"] == "limit_exceeded"


@pytest.mark.anyio
async def test_cli_stdio_real_subprocess_session():
    import os
    import sys

    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

    params = StdioServerParameters(
        command=sys.executable,
        args=["-m", "iacpaas_mcp.server"],
        env={**os.environ, "PYTHONPATH": "src"},
    )
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            assert len((await session.list_tools()).tools) == 9
            result = await session.call_tool("count_graph", {"path": "/demo/university"})
            assert not result.isError
            assert result.structuredContent["backend"] == "mock"


@pytest.mark.anyio
async def test_write_warning_is_inside_result_byte_budget(monkeypatch):
    import json

    from iacpaas_mcp.server import create_server

    class TinyService:
        def update_concept(self, **kwargs):
            return {}

    monkeypatch.setenv("IACPAAS_ALLOW_WRITES", "1")
    monkeypatch.setenv("IACPAAS_WRITABLE_PREFIX", "/demo/")
    result = await create_server(TinyService(), max_result_bytes=100).call_tool(
        "update_concept", {"path": "/demo/x", "concept_id": "root"}
    )
    from mcp.types import CallToolResult

    assert isinstance(result, CallToolResult), "warning bypassed result byte limit"
    assert result.isError
    assert json.loads(result.content[0].text)["code"] == "limit_exceeded"


@pytest.mark.anyio
@pytest.mark.parametrize(
    "failure,code,retryable",
    [
        (TimeoutError("internal token should not leak"), "timeout", True),
        (RuntimeError("internal token should not leak"), "platform_error", False),
    ],
)
async def test_execution_error_categories_are_sanitized(failure, code, retryable):
    import json

    from iacpaas_mcp.server import create_server

    class FailingService:
        def get_inforesource(self, **kwargs):
            raise failure

    async with create_connected_server_and_client_session(
        create_server(FailingService())
    ) as session:
        result = await session.call_tool("get_inforesource", {"path": "/demo/x"})
        assert result.isError
        error = json.loads(result.content[0].text)
        assert error["code"] == code
        assert error["retryable"] is retryable
        assert "internal token" not in error["message"]


@pytest.mark.anyio
async def test_mock_import_roundtrip_requires_startup_opt_in(monkeypatch):
    from iacpaas_mcp.server import create_server

    monkeypatch.setenv("IACPAAS_ALLOW_WRITES", "1")
    monkeypatch.setenv("IACPAAS_WRITABLE_PREFIX", "/demo/")
    async with create_connected_server_and_client_session(create_server()) as session:
        exported = await session.call_tool("export_inforesource", {"path": "/demo/university"})
        payload = exported.structuredContent["data"]
        payload["path"] = "/demo/imported"
        result = await session.call_tool("import_inforesource", {"payload": payload})
        assert not result.isError, result
        loaded = await session.call_tool("export_inforesource", {"path": "/demo/imported"})
        assert not loaded.isError
        assert loaded.structuredContent["data"] == payload


def test_mock_writes_are_serialized_across_direct_callers(monkeypatch):
    import asyncio
    import time
    from concurrent.futures import ThreadPoolExecutor

    from iacpaas_mcp.server import create_server

    class ConcurrentService:
        active = 0
        maximum = 0
        calls = 0

        def update_concept(self, **kwargs):
            self.active += 1
            self.maximum = max(self.maximum, self.active)
            time.sleep(0.01)
            self.calls += 1
            self.active -= 1
            return {"updated": True}

    monkeypatch.setenv("IACPAAS_ALLOW_WRITES", "1")
    monkeypatch.setenv("IACPAAS_WRITABLE_PREFIX", "/demo/")
    service = ConcurrentService()
    server = create_server(service)

    def invoke(_):
        return asyncio.run(
            server.call_tool("update_concept", {"path": "/demo/x", "concept_id": "root"})
        )

    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(invoke, range(8)))
    assert len(results) == 8
    assert service.calls == 8  # one invocation each; no automatic retries
    assert service.maximum == 1


def test_cli_rejects_real_backend_without_silent_mock():
    import os
    import subprocess
    import sys

    result = subprocess.run(
        [sys.executable, "-m", "iacpaas_mcp.server", "--backend", "iacpaas"],
        env={**os.environ, "PYTHONPATH": "src"},
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert result.returncode != 0
    assert "unsupported_backend" in result.stderr
    assert not result.stdout
