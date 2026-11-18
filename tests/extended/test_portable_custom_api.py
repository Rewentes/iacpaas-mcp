"""User-selected Chat Completions endpoints keep keys and contexts isolated."""

import asyncio
import hashlib
import json
import sqlite3

import httpx
import pytest
from starlette.testclient import TestClient
from test_portable_authored_conversation import gas_session
from test_portable_reconciliation import review_report

from gas_web.api_endpoint import normalize_base_url
from gas_web.app import create_app
from gas_web.model_settings import ModelSettings
from gas_web.storage import Storage
from standalone_agent.conversation import ConversationAgent
from standalone_agent.errors import ConversationError
from standalone_agent.prompts import REVIEW_PROMPT, summary_prompt
from standalone_agent.working_memory import working_summary_prompt


@pytest.mark.parametrize(
    "value,expected",
    [
        (" https://vendor.example/v1/ ", "https://vendor.example/v1"),
        ("https://vendor.example/api/v1/chat/completions", "https://vendor.example/api/v1"),
        ("http://127.0.0.1:1234/v1", "http://127.0.0.1:1234/v1"),
        ("http://host.docker.internal:1234/v1/", "http://host.docker.internal:1234/v1"),
        ("http://[::1]:1234/v1", "http://[::1]:1234/v1"),
    ],
)
def test_endpoint_normalization(value, expected):
    assert normalize_base_url(value) == expected


@pytest.mark.parametrize(
    "value",
    [
        "",
        None,
        "vendor.example/v1",
        "file:///tmp/private",
        "https://key@vendor.example/v1",
        "https://vendor.example/v1?api_key=secret",
        "https://vendor.example/v1#secret",
        "https://vendor.example:bad/v1",
        "https://vendor.example\\other/v1",
        "http://169.254.169.254/v1",
        "http://0.0.0.0/v1",
        "http://224.0.0.1/v1",
    ],
)
def test_invalid_endpoint_does_not_echo_input(value):
    with pytest.raises(ValueError) as error:
        normalize_base_url(value)
    assert "secret" not in str(error.value) and "private" not in str(error.value)


def test_profile_migration_preserves_existing_keys_and_test_status(tmp_path):
    path = tmp_path / "old.db"
    store = Storage(path)
    owner, _ = store.new_owner()
    signature = hashlib.sha256(json.dumps(["old-model", "old-key"]).encode()).hexdigest()
    with sqlite3.connect(path) as db:
        db.execute(
            "CREATE TABLE model_profiles (owner_id TEXT, provider TEXT, model TEXT, api_key TEXT, connection_status TEXT, PRIMARY KEY(owner_id,provider))"
        )
        db.execute(
            "INSERT INTO model_profiles VALUES (?,?,?,?,?)",
            (owner["id"], "openrouter", "old-model", "old-key", "connected:" + signature),
        )
    settings = ModelSettings(store)
    profile = settings.snapshot(owner["id"])
    assert profile.model == "old-model" and profile.api_key == "old-key"
    assert profile.connection_status == "connected"
    ModelSettings(store)  # Migration is repeatable.


def test_endpoint_changes_clear_only_custom_key_and_invalidate_old_test(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "official-openai-secret")
    monkeypatch.setenv("OPENROUTER_API_KEY", "router-secret")
    settings = ModelSettings(Storage(tmp_path / "settings.db"))
    owner, _ = settings.store.new_owner()
    other, _ = settings.store.new_owner()
    settings.save(
        owner["id"],
        {
            "provider": "custom",
            "model": "vendor-model",
            "base_url": "https://one.example/v1/chat/completions",
            "api_key": "vendor-secret",
        },
    )
    original = settings.snapshot(owner["id"])
    assert original.base_url == "https://one.example/v1"
    settings.save(
        owner["id"],
        {"provider": "custom", "model": "vendor-model", "base_url": "https://one.example/v1/"},
    )
    assert settings.snapshot(owner["id"]).api_key == "vendor-secret"
    assert settings.snapshot(other["id"], "custom").api_key == ""
    settings.save(
        owner["id"],
        {"provider": "custom", "model": "vendor-model", "base_url": "https://two.example/v1"},
    )
    updated = settings.snapshot(owner["id"])
    assert updated.api_key == "" and updated.base_url == "https://two.example/v1"
    settings.record_test(original, "connected")
    assert settings.snapshot(owner["id"]).connection_status == "configured"
    assert settings.snapshot(owner["id"], "openai").api_key == "official-openai-secret"
    assert settings.snapshot(owner["id"], "openrouter").api_key == "router-secret"


def mock_client(monkeypatch, respond):
    original = httpx.AsyncClient

    def client(*args, **kwargs):
        kwargs["transport"] = httpx.MockTransport(respond)
        return original(*args, **kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", client)


def test_custom_api_serves_catalog_tools_review_and_context_summary(tmp_path, monkeypatch):
    settings = ModelSettings(Storage(tmp_path / "settings.db"))
    owner, _ = settings.store.new_owner()
    settings.save(
        owner["id"],
        {
            "provider": "custom",
            "model": "vendor-model",
            "base_url": "https://vendor.example/v1",
            "api_key": "vendor-secret",
        },
    )
    profile = settings.snapshot(owner["id"])
    calls = []
    writes = 0

    def respond(request):
        nonlocal writes
        calls.append(request)
        assert request.url.host == "vendor.example"
        assert request.headers["Authorization"] == "Bearer vendor-secret"
        if request.method == "GET":
            assert request.url.path == "/v1/models"
            return httpx.Response(200, json={"data": [{"id": "vendor-model"}]})
        assert request.url.path == "/v1/chat/completions"
        body = json.loads(request.content)
        assert body["model"] == "vendor-model" and "max_tokens" in body
        assert "seed" not in body and "response_format" not in body
        messages = body["messages"]
        prompt = messages[0]["content"]
        if prompt == summary_prompt(256):
            message = {"content": "Обсуждали чистоту аргона. Числа нужно прочитать заново."}
        elif prompt == working_summary_prompt(256):
            message = {"content": "Карточка аргона прочитана, продолжить проверку."}
        elif prompt == REVIEW_PROMPT:
            message = review_report(messages)
        else:
            writes += 1
            if writes == 1:
                assert body["tools"]
                message = {
                    "content": "Прочитаю карточку.",
                    "tool_calls": [
                        {
                            "id": "read",
                            "type": "function",
                            "function": {"name": "get_gas_specification", "arguments": "{}"},
                        }
                    ],
                }
            else:
                packet = json.loads(messages[-1]["content"])
                source = next(
                    s["id"]
                    for s in packet.get("evidence", packet.get("sources", []))
                    if s.get("value_kind") == "normative_limit"
                )
                message = {"content": f"Аргон: ≥ 99.993 %. [{source}]"}
        return httpx.Response(
            200, json={"choices": [{"message": {"role": "assistant", **message}}]}
        )

    mock_client(monkeypatch, respond)

    async def run():
        assert (await settings.models(owner["id"], "custom"))["items"] == [
            {"id": "vendor-model", "name": "vendor-model"}
        ]
        agent = ConversationAgent(
            provider=settings.provider(profile),
            reviewer=settings.provider(profile, review=True),
            session_factory=gas_session,
            context_limit=9000,
            output_reserve=256,
            summary_bytes=256,
            tool_result_bytes=2048,
        )
        history = [
            {
                "id": str(i),
                "role": "user" if i % 2 == 0 else "assistant",
                "content": "Обсуждаем аргон. " * 30,
            }
            for i in range(12)
        ]
        return await agent.respond("Какая чистота аргона?", history)

    result = asyncio.run(run())
    assert result["verification"]["status"] == "reviewed"
    assert result["summary_update"] and result["events"]
    prompts = [json.loads(r.content)["messages"][0]["content"] for r in calls if r.method == "POST"]
    assert summary_prompt(256) in prompts and REVIEW_PROMPT in prompts


def test_custom_local_api_allows_no_key_and_never_follows_redirects(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "official-secret")
    settings = ModelSettings(Storage(tmp_path / "settings.db"))
    owner, _ = settings.store.new_owner()
    settings.save(
        owner["id"],
        {"provider": "custom", "model": "local-model", "base_url": "http://localhost:1234/v1"},
    )
    calls = []

    def respond(request):
        calls.append(request)
        assert "Authorization" not in request.headers
        return httpx.Response(302, headers={"Location": "https://other.example/private"})

    mock_client(monkeypatch, respond)
    with pytest.raises(ConversationError):
        asyncio.run(
            settings.provider(settings.snapshot(owner["id"]))(
                [{"role": "user", "content": "Synthetic"}], []
            )
        )
    assert len(calls) == 1 and calls[0].url.host == "localhost"


def test_public_custom_profile_roundtrip_and_owner_isolation(tmp_path, monkeypatch):
    async def codex_status(*args):
        return {"installed": False, "connected": False, "login_pending": False}

    monkeypatch.setattr(ModelSettings, "codex_status", codex_status)
    app = create_app(db_path=tmp_path / "chat.db")
    with TestClient(app) as client, TestClient(app) as other:
        client.headers["X-CSRF-Token"] = client.get("/api/me").json()["csrf_token"]
        other.headers["X-CSRF-Token"] = other.get("/api/me").json()["csrf_token"]
        body = {
            "provider": "custom",
            "base_url": "https://vendor.example/v1/chat/completions",
            "model": "vendor-model",
            "api_key": "vendor-secret",
        }
        response = client.post("/api/settings", json=body)
        assert (
            response.status_code == 200
            and response.json()["base_url"] == "https://vendor.example/v1"
        )
        assert response.json()["api_key_set"] and "vendor-secret" not in response.text
        assert (
            client.post(
                "/api/settings", json={"provider": "openrouter", "model": "router-model"}
            ).status_code
            == 200
        )
        draft = client.get("/api/settings?provider=custom").json()
        assert draft["base_url"] == "https://vendor.example/v1" and draft["api_key_set"]
        assert client.get("/api/settings").json()["provider"] == "openrouter"
        foreign = other.get("/api/settings?provider=custom").json()
        assert not foreign["base_url"] and not foreign["api_key_set"]
        assert client.get("/api/settings?provider=unknown").status_code == 400
        body["base_url"] = "https://vendor.example/v1?token=secret"
        invalid = client.post("/api/settings", json=body)
        assert invalid.status_code == 400 and "secret" not in invalid.text
