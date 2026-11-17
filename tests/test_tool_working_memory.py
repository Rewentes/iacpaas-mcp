"""Summaries progress in bounded batches; originals and cancellation remain intact."""

import asyncio
import copy
import json

import pytest

from standalone_agent.conversation import estimate
from standalone_agent.working_memory import WorkingMemory, working_summary_prompt


async def emit(*args):
    pass


def test_model_summarizes_incremental_batches_without_mutating_originals():
    memory = WorkingMemory()
    events = [{"name": "read", "result": {"item": i, "description": "a" * 1400}} for i in range(4)]
    original = copy.deepcopy(events)
    requests = []

    async def provider(messages, tools):
        assert tools == [] and messages[0]["content"] == working_summary_prompt(256)
        packet = json.loads(messages[-1]["content"])
        requests.append(packet)
        if len(requests) > 1:
            assert (
                packet["previous_working_summary"] == "Объекты из предыдущих результатов прочитаны."
            )
        return {"content": "Объекты из предыдущих результатов прочитаны."}

    asyncio.run(
        memory.compress(
            "Учебный вопрос",
            events,
            provider,
            lambda r: estimate(r, []) < 4000,
            256,
            emit,
        )
    )
    assert memory.failure is None and memory.covered_events == 4
    assert 1 < memory.requests <= 4
    assert [e for p in requests for e in p["tool_events"]] == original
    assert events == original
    before = memory.requests
    asyncio.run(memory.compress("Учебный вопрос", events, provider, lambda r: True, 256, emit))
    assert memory.requests == before  # Already-covered events are not summarized again.


@pytest.mark.parametrize(
    "response",
    [
        None,
        {"content": ""},
        {"content": "я" * 200},
        {"content": "summary", "tool_calls": [{"id": "bad"}]},
    ],
)
def test_invalid_summary_is_not_installed_or_retried(response):
    memory = WorkingMemory()

    async def provider(messages, tools):
        return response

    events = [{"name": "read", "result": {"value": 1}}]
    for _ in range(2):
        asyncio.run(memory.compress("Вопрос", events, provider, lambda r: True, 256, emit))
    assert memory.text == "" and memory.covered_events == 0
    assert memory.requests == 1 and memory.failure == "invalid_working_summary"


def test_summary_input_budget_does_not_send_oversized_events():
    memory = WorkingMemory()

    async def provider(*args):
        pytest.fail("Oversized input must not be sent")

    asyncio.run(
        memory.compress("Вопрос", [{"result": "large"}], provider, lambda r: False, 256, emit)
    )
    assert memory.failure == "summary_input_limit" and memory.requests == 0


def test_provider_failure_is_safe_and_cancellation_is_not_swallowed():
    async def failed(*args):
        raise RuntimeError("Private provider details")

    memory = WorkingMemory()
    asyncio.run(memory.compress("Вопрос", [{"result": "data"}], failed, lambda r: True, 256, emit))
    assert memory.failure == "summary_provider_error"
    assert "Private" not in json.dumps(memory.snapshot())

    async def cancelled(*args):
        raise asyncio.CancelledError

    with pytest.raises(asyncio.CancelledError):
        asyncio.run(
            WorkingMemory().compress(
                "Вопрос", [{"result": "data"}], cancelled, lambda r: True, 256, emit
            )
        )
