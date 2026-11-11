"""Provider envelopes must never turn truncated output into a verified answer."""

import asyncio
import json

import httpx
import pytest
from starlette.testclient import TestClient

from gas_web.app import create_app
from gas_web.model_settings import ModelSettings
from gas_web.storage import Storage
from standalone_agent.completion import completion_message
from standalone_agent.errors import ConversationError
from standalone_agent.models import OpenAIModel


def envelope(content="Ответ", finish="stop", **kwargs):
    return {
        "choices": [
            {
                "finish_reason": finish,
                "message": {
                    "role": "assistant",
                    "content": content,
                    **kwargs,
                },
            }
        ]
    }


def test_text_parts_are_joined_but_private_reasoning_is_never_returned():
    result = completion_message(
        envelope(
            [{"type": "text", "text": "Аргон "}, {"type": "text", "text": "[e1]"}],
            reasoning="PRIVATE",
            reasoning_details=[{"text": "PRIVATE"}],
        )
    )
    assert result == {"role": "assistant", "content": "Аргон [e1]"}
    with pytest.raises(ConversationError):
        completion_message(envelope([{"type": "reasoning", "text": "PRIVATE"}]))


@pytest.mark.parametrize("content", [None, "", "   "])
def test_empty_content_has_its_own_failure_category(content):
    with pytest.raises(ConversationError) as error:
        completion_message(envelope(content))
    assert error.value.code == "empty_model_output"


@pytest.mark.parametrize(
    "data,code",
    [
        (envelope('{"text":"prefix', "length"), "model_output_limit"),
        (envelope(None, "length", tool_calls=[{"id": "a"}]), "model_output_limit"),
        (envelope("prefix", "content_filter"), "model_refusal"),
        (envelope(None, refusal="PRIVATE"), "model_refusal"),
        ({"choices": []}, "model_output_error"),
        ({"choices": [{"message": None}]}, "model_output_error"),
    ],
)
def test_incomplete_or_refused_outputs_are_not_publishable(data, code):
    with pytest.raises(ConversationError) as error:
        completion_message(data)
    assert error.value.code == code
    assert "PRIVATE" not in str(error.value)


@pytest.mark.parametrize("adapter", ["cli", "web"])
def test_transports_preserve_length_failure_without_retry(tmp_path, monkeypatch, adapter):
    calls = []
    response = envelope('{"claims": []}', "length")

    async def run():
        if adapter == "cli":

            def respond(request):
                if request.method == "GET":
                    return httpx.Response(200, json={"data": []})
                calls.append(request)
                return httpx.Response(200, json=response)

            async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
                await OpenAIModel(
                    "https://openrouter.ai/api/v1", "key", "model", client=client
                ).complete([{"role": "user", "content": "Вопрос"}], [])
        else:
            from gas_web import model_settings

            async def request(*args, **kwargs):
                calls.append(kwargs)
                return response

            async def sampling(model):
                return {}
            monkeypatch.setattr(model_settings, "openrouter_sampling_options", sampling)
            monkeypatch.setattr(model_settings, "api_request", request)
            monkeypatch.setenv("OPENROUTER_API_KEY", "test-key")
            settings = ModelSettings(Storage(tmp_path / "settings.db"))
            owner, _ = settings.store.new_owner()
            await settings.provider(settings.snapshot(owner["id"]))(
                [{"role": "user", "content": "Вопрос"}], []
            )

    with pytest.raises(ConversationError) as error:
        asyncio.run(run())
    assert error.value.code == "model_output_limit"
    assert len(calls) == 1


def test_web_reports_actual_limit_failure_and_keeps_original_message(tmp_path):
    class Agent:
        calls = 0

        async def respond(self, *args, **kwargs):
            self.calls += 1
            completion_message(envelope("неполный текст", "length"))

    agent = Agent()
    with TestClient(
        create_app(db_path=tmp_path / "history.db", agent=agent), base_url="http://127.0.0.1"
    ) as client:
        client.headers["X-CSRF-Token"] = client.get("/api/me").json()["csrf_token"]
        chat = client.post("/api/conversations", json={"title": "Тест"}).json()["id"]
        url = f"/api/conversations/{chat}/messages"
        request = {"request_id": "one", "content": "Покажи газы"}
        response = client.post(url, json=request)
        assert response.status_code == 502
        assert response.json()["error"]["code"] == "model_output_limit"
        assert "неполный текст" not in json.dumps(response.json())
        assert client.post(url, json=request).json() == response.json()
        history = client.get(url).json()["messages"]
        assert len(history) == 1 and history[0]["content"] == request["content"]
        assert history[0]["state"] == "failed" and agent.calls == 1
