"""Interactive model selection and safe transport failures without inference."""

import asyncio

import httpx
import pytest
from starlette.testclient import TestClient

from gas_web import model_settings
from gas_web.app import create_app
from gas_web.model_settings import ModelSettings, Profile
from gas_web.storage import Storage
from standalone_agent.completion import completion_message
from standalone_agent.conversation import ConversationAgent
from standalone_agent.errors import ConversationError
from standalone_agent.models import OpenAIModel
from standalone_agent.provider_errors import is_batch_model


@pytest.mark.parametrize("adapter", ["web", "cli", "direct"])
@pytest.mark.parametrize(
    "status,code",
    [
        (400, "provider_request_error"),
        (401, "provider_auth_error"),
        (403, "provider_access_denied"),
        (402, "provider_credit_limit"),
        (404, "provider_model_unavailable"),
        (429, "provider_rate_limit"),
        (503, "provider_unavailable"),
    ],
)
def test_provider_http_failure_is_specific_safe_and_not_retried(
    tmp_path, monkeypatch, adapter, status, code
):
    posts = []

    def respond(request):
        if request.method == "GET":
            return httpx.Response(200, json={"data": []})
        posts.append(request)
        return httpx.Response(status, json={"error": {"message": "PRIVATE-body test-key"}})

    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
            if adapter == "web":
                settings = ModelSettings(Storage(tmp_path / "db.sqlite3"))
                profile = Profile("owner", "openrouter", "fixture-model", "test-key")

                async def api(*args, **kwargs):
                    response = await client.post("https://openrouter.ai/api/v1/chat/completions")
                    response.raise_for_status()
                    return response.json()

                async def sampling(model):
                    return {}

                monkeypatch.setattr(model_settings, "api_request", api)
                monkeypatch.setattr(model_settings, "openrouter_sampling_options", sampling)
                complete = settings.provider(profile)
            elif adapter == "cli":
                complete = OpenAIModel(
                    "https://openrouter.ai/api/v1", "test-key", "fixture-model", client=client
                ).complete
            else:
                monkeypatch.setenv("OPENROUTER_API_KEY", "test-key")
                monkeypatch.setenv("OPENROUTER_MODEL", "fixture-model")
                complete = ConversationAgent(client=client)._complete
            await complete([{"role": "user", "content": "Synthetic question"}], [])

    with pytest.raises(ConversationError) as error:
        asyncio.run(run())
    assert error.value.code == code
    assert error.value.diagnostic == f"provider_http_{status}"
    assert "PRIVATE" not in str(error.value) and "test-key" not in str(error.value)
    assert len(posts) == 1


def test_provider_timeout_is_distinct_from_unsupported_output(tmp_path, monkeypatch):
    async def fail(*args, **kwargs):
        raise httpx.ReadTimeout("PRIVATE upstream timeout")

    async def sampling(model):
        return {}

    monkeypatch.setattr(model_settings, "api_request", fail)
    monkeypatch.setattr(model_settings, "openrouter_sampling_options", sampling)
    settings = ModelSettings(Storage(tmp_path / "db.sqlite3"))
    with pytest.raises(ConversationError) as error:
        asyncio.run(
            settings.provider(Profile("owner", "openrouter", "fixture-model", "key"))([], [])
        )
    assert error.value.code == "provider_timeout" and "PRIVATE" not in str(error.value)


@pytest.mark.parametrize(
    "status,code",
    [
        (402, "provider_credit_limit"),
        ("429", "provider_rate_limit"),
        ("upstream_error", "provider_response_error"),
        (True, "provider_response_error"),
    ],
)
def test_error_inside_http_success_never_becomes_a_generic_parse_failure(status, code):
    with pytest.raises(ConversationError) as error:
        completion_message({"error": {"code": status, "message": "PRIVATE"}})
    assert error.value.code == code and "PRIVATE" not in str(error.value)


def test_batch_variants_are_filtered_and_rejected_without_replacing_profile(tmp_path, monkeypatch):
    settings = ModelSettings(Storage(tmp_path / "db.sqlite3"))
    owner, _ = settings.store.new_owner()
    settings.save(
        owner["id"], {"provider": "openrouter", "model": "vendor/model", "api_key": "key"}
    )

    async def api(*args, **kwargs):
        assert args[1] == "/models" and "payload" not in kwargs
        return {
            "data": [
                {"id": item}
                for item in [
                    "vendor/model",
                    "vendor/model:batch",
                    "vendor/model:free",
                    "vendor/batch-model",
                ]
            ]
        }

    monkeypatch.setattr(model_settings, "api_request", api)
    items = asyncio.run(settings.models(owner["id"], "openrouter"))["items"]
    assert [item["id"] for item in items] == [
        "vendor/model",
        "vendor/model:free",
        "vendor/batch-model",
    ]
    with pytest.raises(ValueError, match=":batch"):
        settings.save(owner["id"], {"provider": "openrouter", "model": "vendor/model:batch"})
    assert settings.snapshot(owner["id"]).model == "vendor/model"
    assert not is_batch_model("openai", "vendor/model:batch")
    with pytest.raises(ConversationError) as error:
        settings.provider(Profile(owner["id"], "openrouter", "vendor/model:batch", "key"))
    assert error.value.code == "provider_mode_unsupported"


def test_old_batch_profile_gets_warning_and_web_failure_without_mcp_or_inference(
    tmp_path, monkeypatch
):
    path = tmp_path / "chat.sqlite3"

    async def forbidden(*args, **kwargs):
        pytest.fail("Batch preflight must not call the provider")

    monkeypatch.setattr(model_settings, "api_request", forbidden)
    with TestClient(create_app(db_path=path)) as client:
        client.headers["X-CSRF-Token"] = client.get("/api/me").json()["csrf_token"]
        assert (
            client.post(
                "/api/settings",
                json={"provider": "openrouter", "model": "vendor/model", "api_key": "test-key"},
            ).status_code
            == 200
        )
        # A profile saved by an older version remains visible until the owner changes it.
        with client.app.state.storage.connect() as db:
            db.execute("UPDATE model_profiles SET model='vendor/model:batch'")
        public = client.get("/api/settings").json()
        assert public["model"].endswith(":batch") and ":batch" in public["model_warning"]
        chat = client.post("/api/conversations", json={"title": "Synthetic"}).json()["id"]
        uri = f"/api/conversations/{chat}/messages"
        body = {"request_id": "q1", "content": "Synthetic question"}
        response = client.post(uri, json=body)
        assert response.status_code == 502
        assert response.json()["error"]["code"] == "provider_mode_unsupported"
        assert client.post(uri, json=body).json() == response.json()
        history = client.get(uri).json()["messages"]
        assert len(history) == 1 and history[0]["content"] == body["content"]
        assert history[0]["state"] == "failed" and "test-key" not in response.text
        assert (
            client.post(
                "/api/settings", json={"provider": "openrouter", "model": "vendor/model"}
            ).status_code
            == 200
        )
        public = client.get("/api/settings").json()
        assert public["model_warning"] is None and public["api_key_set"]
