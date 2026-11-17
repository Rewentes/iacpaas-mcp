"""UI settings payloads reach the provider unchanged; no real keys or inference."""

import json

import httpx
import pytest
from starlette.testclient import TestClient

from gas_web import model_settings
from gas_web.app import create_app


@pytest.mark.parametrize(
    "status,code", [(200, None), (401, "provider_auth_error"), (403, "provider_access_denied")]
)
def test_saved_ui_key_overrides_env_and_survives_empty_save_and_failed_test(
    tmp_path, monkeypatch, status, code
):
    ui_key, env_key = "synthetic-ui-key-12345", "synthetic-env-key-67890"
    monkeypatch.setenv("OPENROUTER_API_KEY", env_key)
    posts, catalog_headers = [], []

    def respond(request):
        if request.method == "GET":
            catalog_headers.append(request.headers)
            return httpx.Response(200, json={"data": [{"id": "fixture-key-model"}]})
        posts.append(request)
        if status != 200:
            return httpx.Response(status, json={"error": {"message": "PRIVATE upstream body"}})
        return httpx.Response(
            200, json={"choices": [{"message": {"role": "assistant", "content": "OK"}}]}
        )

    original_client = httpx.AsyncClient

    def fake_client(*args, **kwargs):
        kwargs["transport"] = httpx.MockTransport(respond)
        return original_client(*args, **kwargs)

    async def codex_status(*args):
        return {"installed": False, "connected": False, "login_pending": False}

    monkeypatch.setattr(httpx, "AsyncClient", fake_client)
    monkeypatch.setattr(model_settings.ModelSettings, "codex_status", codex_status)
    with TestClient(create_app(db_path=tmp_path / "chat.sqlite3")) as client:
        client.headers["X-CSRF-Token"] = client.get("/api/me").json()["csrf_token"]
        body = {
            "provider": "openrouter",
            "model": "fixture-key-model",
            "api_key": " " + ui_key + " ",
        }
        saved = client.post("/api/settings", json=body)
        assert saved.status_code == 200 and saved.json()["api_key_set"]
        assert ui_key not in saved.text and env_key not in saved.text
        body["api_key"] = ""
        assert client.post("/api/settings", json=body).json()["api_key_set"]
        catalog = client.get("/api/models?provider=openrouter")
        assert catalog.status_code == 200
        tested = client.post("/api/settings/test", json={})
        if code:
            assert tested.status_code == 502 and tested.json()["error"]["code"] == code
        else:
            assert tested.status_code == 200 and tested.json()["ok"]
        assert "PRIVATE" not in tested.text and ui_key not in tested.text
        assert client.get("/api/settings").json()["api_key_set"]
        assert len(posts) == 1 and posts[0].headers["Authorization"] == "Bearer " + ui_key
        assert json.loads(posts[0].content)["model"] == "fixture-key-model"
        assert all("authorization" not in headers for headers in catalog_headers)
