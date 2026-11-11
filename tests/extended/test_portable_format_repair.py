"""Malformed final prose can be corrected once without repeating MCP reads."""

import asyncio
import json
from contextlib import asynccontextmanager
from types import SimpleNamespace

import pytest
from test_portable_authored_conversation import accepted_review, gas_session

from standalone_agent.conversation import ConversationAgent, ConversationError


@pytest.mark.parametrize("broken", ['{"answer":', "Аргон [e1-e100000000]", "Аргон [e1 wrong]"])
def test_one_format_correction_keeps_evidence_and_still_requires_review(broken):
    generation = []
    reads = []
    reviews = []
    progress = []

    @asynccontextmanager
    async def tracked_session():
        async with gas_session() as session:
            original = session.call_tool

            async def read(name, arguments):
                reads.append(name)
                return await original(name, arguments)

            session.call_tool = read
            yield session

    async def provider(messages, tools):
        generation.append((messages, tools))
        if len(generation) == 1:
            return {
                "content": "Посмотрю газ.",
                "tool_calls": [
                    {
                        "id": "read",
                        "type": "function",
                        "function": {"name": "get_gas_specification", "arguments": "{}"},
                    }
                ],
            }
        if len(generation) == 2:
            return {"content": broken}
        assert tools == []
        tool_result = next(m for m in reversed(messages) if m["role"] == "tool")
        source = next(
            s
            for s in json.loads(tool_result["content"])["evidence"]
            if s.get("value_kind") == "normative_limit"
        )
        return {"content": f"Аргон высший сорт [{source['id']}]"}

    async def review(messages, tools):
        reviews.append(messages)
        return await accepted_review(messages, tools)

    async def emit(event):
        progress.append(event)

    result = asyncio.run(
        ConversationAgent(
            provider=provider,
            reviewer=review,
            session_factory=tracked_session,
        ).respond("Что есть по аргону?", [], on_event=emit)
    )
    assert len(generation) == 3 and reads == ["get_gas_specification"]
    assert len(reviews) == 1 and result["verification"]["status"] == "reviewed"
    assert result["answer"].startswith("Аргон высший сорт")
    assert any("Исправляю оформление" in p["text"] for p in progress)
    assert len(result["events"]) == 1 and result["sources"]


@pytest.mark.parametrize(
    "repair",
    [
        {"content": '{"answer":'},
        {
            "content": "",
            "tool_calls": [{"id": "forbidden", "function": {"name": "read", "arguments": "{}"}}],
        },
    ],
)
def test_unsuccessful_correction_is_bounded_and_cannot_run_tools(repair):
    calls = []

    @asynccontextmanager
    async def empty_session():
        yield SimpleNamespace(list_tools=tools)

    async def tools():
        return SimpleNamespace(tools=[])

    async def provider(messages, tools):
        calls.append(messages)
        return {"content": '{"answer":'} if len(calls) == 1 else repair

    with pytest.raises(ConversationError) as error:
        asyncio.run(
            ConversationAgent(provider=provider, session_factory=empty_session).respond(
                "Вопрос",
                [],
            )
        )
    assert error.value.code == "model_output_error" and len(calls) == 2
