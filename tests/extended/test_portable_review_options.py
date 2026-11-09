import asyncio
import json

import httpx
import pytest

from standalone_agent.models import OpenAIModel
from standalone_agent.review_options import reasoning_options


@pytest.mark.parametrize(
    "metadata,expected",
    [
        ({}, {}),
        (
            {
                "reasoning": {
                    "supported_efforts": ["max", "high", "low", "none"],
                    "mandatory": False,
                }
            },
            {"reasoning": {"enabled": False}},
        ),
        (
            {"reasoning": {"supported_efforts": ["high", "minimal"], "mandatory": True}},
            {"reasoning": {"effort": "minimal"}},
        ),
        ({"reasoning": {"supported_efforts": ["none"], "mandatory": True}}, {}),
        (
            {"reasoning": {"supported_efforts": ["none"], "mandatory": False}},
            {"reasoning": {"enabled": False}},
        ),
    ],
)
def test_review_uses_only_exposed_model_controls(metadata, expected):
    assert reasoning_options(metadata) == expected


def test_cli_transport_sets_reasoning_only_for_the_review(monkeypatch):
    from standalone_agent import review_options

    monkeypatch.setattr(review_options, "_CACHE", {})
    calls = []

    def respond(request):
        calls.append(request)
        if request.method == "GET":
            return httpx.Response(
                200,
                json={
                    "data": [
                        {
                            "id": "test-review-model",
                            "reasoning": {"supported_efforts": ["high", "low"]},
                        }
                    ]
                },
            )
        return httpx.Response(
            200, json={"choices": [{"message": {"role": "assistant", "content": "Ответ"}}]}
        )

    async def scenario():
        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
            model = OpenAIModel(
                "https://openrouter.ai/api/v1", "test-key", "test-review-model", client=client
            )
            await model.complete([{"role": "user", "content": "Вопрос"}], [])
            await model.review([{"role": "user", "content": "Сверка"}], [])

    asyncio.run(scenario())
    posts = [json.loads(call.content) for call in calls if call.method == "POST"]
    assert "reasoning" not in posts[0]
    assert posts[1]["reasoning"] == {"effort": "low"}


def test_web_transport_passes_the_same_review_controls(tmp_path, monkeypatch):
    from gas_web import model_settings
    from gas_web.storage import Storage

    monkeypatch.setenv("OPENROUTER_API_KEY", "scenario-key")
    store = Storage(tmp_path / "settings.db")
    owner, _ = store.new_owner()
    settings = model_settings.ModelSettings(store)
    profile = settings.snapshot(owner["id"])
    requests = []

    async def options(model):
        return {"reasoning": {"effort": "low"}}

    async def api(provider, path, **kwargs):
        requests.append(kwargs["payload"])
        return {"choices": [{"message": {"role": "assistant", "content": "Ответ"}}]}

    monkeypatch.setattr(model_settings, "openrouter_review_options", options)
    monkeypatch.setattr(model_settings, "api_request", api)

    async def scenario():
        await settings.provider(profile)([{"role": "user", "content": "Вопрос"}], [])
        await settings.provider(profile, review=True)([{"role": "user", "content": "Сверка"}], [])

    asyncio.run(scenario())
    assert "reasoning" not in requests[0] and requests[1]["reasoning"] == {"effort": "low"}


def test_review_strict_schema_requires_supported_provider_only():
    from standalone_agent.review_options import review_options

    options = review_options(
        {
            "supported_parameters": ["structured_outputs"],
            "reasoning": {"mandatory": False, "supported_efforts": ["none"]},
        }
    )
    assert options["response_format"]["json_schema"]["strict"] is True
    assert options["response_format"]["json_schema"]["schema"]["additionalProperties"] is False
    assert options["provider"]["require_parameters"] is True
    assert options["reasoning"]["enabled"] is False
    assert "response_format" not in review_options({})
    assert (
        review_options({"supported_parameters": ["response_format"]})["response_format"]["type"]
        == "json_object"
    )
