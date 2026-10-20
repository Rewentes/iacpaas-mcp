import httpx
import pytest


@pytest.mark.anyio
async def test_http_auth_required_and_lifespan_initializes_session(monkeypatch):
    from iacpaas_mcp import server

    assert hasattr(server, "create_http_app")
    monkeypatch.delenv("MCP_HTTP_TOKEN", raising=False)
    with pytest.raises(ValueError, match="MCP_HTTP_TOKEN"):
        server.create_http_app(server.create_server())
    monkeypatch.setenv("MCP_HTTP_TOKEN", "local-test-token")
    instance = server.create_server()
    app = server.create_http_app(instance)
    async with (
        instance.session_manager.run(),
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1:8000"
        ) as client,
    ):
        for headers in [
            {},
            {"Authorization": "Bearer wrong"},
            {"Authorization": "Basic local-test-token"},
        ]:
            response = await client.post("/mcp", headers=headers)
            assert response.status_code == 401
            assert response.headers["www-authenticate"] == "Bearer"
            assert "local-test-token" not in response.text
        headers = {
            "Authorization": "Bearer local-test-token",
            "Accept": "application/json, text/event-stream",
        }
        response = await client.post(
            "/mcp",
            headers=headers,
            json={
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "protocolVersion": "2025-11-25",
                    "capabilities": {},
                    "clientInfo": {"name": "test", "version": "1"},
                },
            },
        )
        assert response.status_code == 200, response.text
        assert response.headers.get("mcp-session-id")
        headers["mcp-session-id"] = response.headers["mcp-session-id"]
        headers["mcp-protocol-version"] = "2025-11-25"
        assert (await client.delete("/mcp", headers=headers)).status_code == 200


def test_http_refuses_nonlocal_binding_without_opt_in(monkeypatch):
    from iacpaas_mcp.server import create_http_app, create_server

    monkeypatch.setenv("MCP_HTTP_TOKEN", "test")
    monkeypatch.delenv("MCP_HTTP_ALLOW_REMOTE", raising=False)
    with pytest.raises(ValueError, match="localhost"):
        create_http_app(create_server(host="0.0.0.0"))
    monkeypatch.setenv("MCP_HTTP_ALLOW_REMOTE", "1")
    assert create_http_app(create_server(host="0.0.0.0")) is not None


def test_sdk_http_factory_cannot_bypass_auth(monkeypatch):
    from iacpaas_mcp.server import create_server

    monkeypatch.delenv("MCP_HTTP_TOKEN", raising=False)
    with pytest.raises(ValueError, match="MCP_HTTP_TOKEN"):
        create_server().streamable_http_app()


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.mark.anyio
async def test_real_http_cli_session_and_graceful_shutdown():
    import asyncio
    import os
    import socket
    import subprocess
    import sys

    import httpx
    from mcp import ClientSession
    from mcp.client.streamable_http import streamable_http_client

    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    process = subprocess.Popen(
        [sys.executable, "-m", "iacpaas_mcp.server", "--transport", "http", "--port", str(port)],
        env={**os.environ, "PYTHONPATH": "src", "MCP_HTTP_TOKEN": "http-session-test"},
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        url = f"http://127.0.0.1:{port}/mcp"
        async with httpx.AsyncClient() as client:
            for _ in range(100):
                try:
                    response = await client.get(url)
                    assert response.status_code == 401
                    break
                except httpx.ConnectError:
                    await asyncio.sleep(0.05)
            else:
                pytest.fail("HTTP CLI did not start")
        async with (
            httpx.AsyncClient(headers={"Authorization": "Bearer http-session-test"}) as http_client,
            streamable_http_client(url, http_client=http_client) as (read, write, get_id),
        ):
            async with ClientSession(read, write) as session:
                await session.initialize()
                assert get_id()
                assert len((await session.list_tools()).tools) == 9
                result = await session.call_tool("count_graph", {"path": "/demo/university"})
                assert not result.isError
                assert result.structuredContent["backend"] == "mock"
    finally:
        process.terminate()
        stdout, stderr = process.communicate(timeout=10)
    assert process.returncode in (0, -15), stderr
    assert "Application shutdown complete" in stderr
    assert not stdout
    assert "http-session-test" not in stderr
