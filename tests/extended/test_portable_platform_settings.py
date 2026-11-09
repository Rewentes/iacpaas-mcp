import asyncio
import os
from contextlib import asynccontextmanager
from types import SimpleNamespace

from starlette.testclient import TestClient

from gas_web.app import create_app
from gas_web.platform_settings import SafeSession


def boot(client):
    client.headers["X-CSRF-Token"] = client.get("/api/me").json()["csrf_token"]


def test_platform_profile_secret_isolation_persistence_and_csrf(tmp_path, monkeypatch):
    monkeypatch.setenv("IACPAAS_USERNAME", "env-login")
    monkeypatch.setenv("IACPAAS_PASSWORD", "env-secret")
    monkeypatch.setenv("MCP_TRANSPORT", "stdio")
    app = create_app(db_path=tmp_path / "chat.db")
    endpoint = "/api/platform/settings"
    with TestClient(app) as client, TestClient(app) as other:
        boot(client)
        boot(other)
        assert client.get(endpoint).json()["source"] == "environment"
        body = {"username": "owner-login", "password": "owner-secret"}
        assert (
            client.post(endpoint, json=body, headers={"X-CSRF-Token": "wrong"}).status_code == 403
        )
        response = client.post(endpoint, json=body)
        assert response.status_code == 200
        assert "owner-secret" not in response.text and "env-secret" not in response.text
        assert other.get(endpoint).json()["username"] == "env-login"
        assert (
            client.post(endpoint, json={"username": "owner-login", "password": ""}).status_code
            == 200
        )
        assert (
            client.post(endpoint, json={"username": "changed", "password": ""}).status_code == 400
        )
        cookies = dict(client.cookies)
    with TestClient(create_app(db_path=tmp_path / "chat.db"), cookies=cookies) as restarted:
        boot(restarted)
        assert restarted.get(endpoint).json()["password_set"] is True
        assert (
            restarted.post(
                endpoint, json={"username": "owner-login", "clear_password": True}
            ).json()["password_set"]
            is False
        )
    assert os.environ["IACPAAS_USERNAME"] == "env-login"


def test_credentials_bound_to_own_mcp_child_and_echo_removed(tmp_path, monkeypatch):
    import gas_web.platform_settings as module

    monkeypatch.setenv("MCP_TRANSPORT", "stdio")
    captured = []

    @asynccontextmanager
    async def fake_connect(*args, **kwargs):
        captured.append(kwargs)

        class Session:
            async def call_tool(self, name, arguments):
                return SimpleNamespace(
                    isError=False, structuredContent={"data": {"x": "user-one secret-one"}}
                )

        yield Session()

    monkeypatch.setattr(module, "connect", fake_connect)
    app = create_app(db_path=tmp_path / "chat.db")
    owner, _ = app.state.storage.new_owner()
    app.state.platform.save(owner["id"], {"username": "user-one", "password": "secret-one"})
    factory = app.state.platform.session_factory(owner["id"])

    async def check():
        async with factory() as session:
            assert isinstance(session, SafeSession)
            result = await session.call_tool("read", {})
            assert result.structuredContent["data"]["x"] == "[REDACTED] [REDACTED]"

    asyncio.run(check())
    assert captured[0]["env_overrides"]["IACPAAS_PASSWORD"] == "secret-one"
    assert captured[0]["env_overrides"]["IACPAAS_TOKEN"] == ""


def test_check_access_is_read_only_and_never_calls_model(tmp_path, monkeypatch):
    import gas_web.platform_settings as module

    monkeypatch.setenv("IACPAAS_BACKEND", "iacpaas")
    monkeypatch.setenv("MCP_TRANSPORT", "stdio")
    calls = []

    class Backend:
        @classmethod
        def from_env(cls, **kwargs):
            calls.append(kwargs)
            return cls()

        def catalog(self):
            return [{"path": "/fixture"}]

        def get(self, path):
            calls.append(path)
            return {"nodes": []}

    monkeypatch.setattr(module, "IACPaaSBackend", Backend)
    app = create_app(db_path=tmp_path / "chat.db")
    with TestClient(app) as client:
        boot(client)
        client.post("/api/platform/settings", json={"username": "test", "password": "password"})
        result = client.post("/api/platform/settings/test", json={})
        assert result.status_code == 200 and result.json()["connected"] is True
    assert calls == [{"username": "test", "password": "password"}, "/fixture"]
