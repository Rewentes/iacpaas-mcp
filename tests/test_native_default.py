"""A fresh checkout reads the real platform or fails; mock must be explicit."""

from pathlib import Path
from types import SimpleNamespace

import pytest
from dotenv import dotenv_values
from starlette.testclient import TestClient

from gas_web import model_settings
from gas_web.app import create_app
from iacpaas_mcp import server
from iacpaas_mcp.config import backend_from_env
from iacpaas_mcp.domain import DomainError


def native_without_credentials(monkeypatch):
    for name in ("IACPAAS_BACKEND", "IACPAAS_USERNAME", "IACPAAS_PASSWORD"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("MCP_TRANSPORT", "stdio")


def test_native_default_and_template_never_fall_back_when_credentials_are_missing(monkeypatch):
    native_without_credentials(monkeypatch)
    assert backend_from_env() == "iacpaas"
    template = Path(__file__).resolve().parents[1] / ".env.example"
    assert dotenv_values(template)["IACPAAS_BACKEND"] == "iacpaas"
    with pytest.raises(DomainError) as error:
        server.create_service()
    assert error.value.code == "configuration_error"
    with pytest.raises(DomainError):
        server.create_server()


def test_mock_still_requires_explicit_choice(monkeypatch):
    native_without_credentials(monkeypatch)
    assert server.create_service("mock").backend.kind == "mock"
    monkeypatch.setenv("IACPAAS_BACKEND", "mock")
    assert server.create_service().backend.kind == "mock"


def test_mcp_cli_uses_native_default_but_accepts_explicit_mock(monkeypatch):
    native_without_credentials(monkeypatch)
    selected, transports = [], []

    def service(backend):
        selected.append(backend)
        return object()

    monkeypatch.setattr(server, "create_service", service)
    monkeypatch.setattr(
        server,
        "create_server",
        lambda *args, **kwargs: SimpleNamespace(
            run=lambda **options: transports.append(options["transport"]),
        ),
    )
    server.main([])
    server.main(["--backend", "mock"])
    assert selected == ["iacpaas", "mock"] and transports == ["stdio", "stdio"]


def test_fresh_web_reports_platform_setup_before_any_inference(tmp_path, monkeypatch):
    native_without_credentials(monkeypatch)
    monkeypatch.setenv("OPENROUTER_API_KEY", "synthetic-model-key")

    async def forbidden(*args, **kwargs):
        pytest.fail("Missing platform credentials must be reported before inference")

    monkeypatch.setattr(model_settings, "api_request", forbidden)
    with TestClient(
        create_app(db_path=tmp_path / "native.db"), base_url="http://127.0.0.1"
    ) as client:
        me = client.get("/api/me").json()
        assert "IACPaaS" in me["backend"] and "mock" not in me["backend"]
        client.headers["X-CSRF-Token"] = me["csrf_token"]
        chat = client.post("/api/conversations", json={"title": "Synthetic"}).json()["id"]
        uri = f"/api/conversations/{chat}/messages"
        response = client.post(uri, json={"request_id": "q1", "content": "Synthetic question"})
        assert response.status_code == 502
        assert response.json()["error"]["code"] == "platform_configuration_error"
        assert "IACPaaS" in response.json()["error"]["message"]
        history = client.get(uri).json()["messages"]
        assert len(history) == 1 and history[0]["state"] == "failed"
