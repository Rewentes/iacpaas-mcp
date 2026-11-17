"""Working evidence can fill a fresh turn even when the transcript is short."""

import asyncio
import copy
import json
from contextlib import asynccontextmanager
from types import SimpleNamespace

from test_portable_reconciliation import review_report

from standalone_agent.conversation import ConversationAgent, estimate
from standalone_agent.prompts import ANSWER_REVISION_NOTE


@asynccontextmanager
async def large_session():
    class Session:
        async def list_tools(self):
            return SimpleNamespace(
                tools=[
                    SimpleNamespace(
                        name="read",
                        description="Synthetic tool description. " * 220,
                        inputSchema={"type": "object"},
                        annotations=SimpleNamespace(readOnlyHint=True),
                    )
                ]
            )

        async def call_tool(self, name, arguments):
            return SimpleNamespace(
                isError=False,
                structuredContent={
                    "ok": True,
                    "backend": "iacpaas",
                    "complete": True,
                    "warnings": ["Synthetic fixture"],
                    "data": {
                        "path": "/fixture",
                        "gas_name": "Учебный газ",
                        "facts": [
                            {
                                "value_kind": "normative_limit",
                                "concept_id": "fixture-grade",
                                "field": "purity",
                                "label": "Учебный газ",
                                "value": 99.5,
                                "unit": "%",
                                "operator": "≥",
                            }
                        ],
                        "description": "A" * 3900,
                    },
                },
            )

    yield Session()


def read(arguments=None):
    return {
        "content": None,
        "tool_calls": [
            {
                "id": "read",
                "type": "function",
                "function": {"name": "read", "arguments": json.dumps(arguments or {})},
            }
        ],
    }


def source_id(packet):
    return next(
        s["id"]
        for s in packet.get("sources", packet.get("evidence", []))
        if s.get("value_kind") == "normative_limit"
    )


async def reviewer(messages, tools):
    return review_report(messages)


def test_revision_reserves_next_result_or_finishes_without_tools():
    requests = []
    evidence = None

    async def writer(messages, tools):
        nonlocal evidence
        requests.append(copy.deepcopy(messages))
        assert estimate(messages, tools) + 512 <= 21000
        if len(requests) == 1:
            return read()
        packet = json.loads(messages[-1]["content"])
        evidence = source_id(packet)
        if len(requests) == 2:
            return {"content": f"Учебный газ: ≥ 90 %. [{evidence}]"}
        assert ANSWER_REVISION_NOTE in messages[0]["content"]
        assert packet["revision_feedback"]["status"] == "rejected"
        # Previously the full feedback fit but another read overflowed. It must
        # either leave room for that read or explicitly turn off tool calling.
        if tools:
            assert estimate(messages, tools) + 512 + 5000 <= 21000
        else:
            assert "Осталось вызовов MCP: 0" in messages[0]["content"]
        return {"content": f"Учебный газ: ≥ 99.5 %. [{evidence}]"}

    result = asyncio.run(
        ConversationAgent(
            provider=writer,
            reviewer=reviewer,
            session_factory=large_session,
            context_limit=21000,
            output_reserve=512,
            tool_result_bytes=5000,
            max_calls=3,
        ).respond("Прочитай учебные данные", [])
    )
    assert result["verification"]["status"] == "reviewed"
    assert result["verification"]["reconciliation"]["revisions"] == 1
    assert len(requests) == 3 and len(result["events"]) == 1
    assert result["events"][0]["result"]["data"]["description"] == "A" * 3900


def test_large_call_envelope_compacts_evidence_and_keeps_followup_context():
    requests = []
    history = [
        {"id": "u0", "role": "user", "content": "Расскажи об учебном газе"},
        {"id": "a0", "role": "assistant", "content": "Обсуждали чистоту учебного газа. [e999]"},
    ]
    original = copy.deepcopy(history)

    async def writer(messages, tools):
        requests.append(copy.deepcopy(messages))
        assert estimate(messages, tools) + 512 <= 21000
        if len(requests) == 1:
            return read({"filter": "x" * 4400})
        assert tools == []
        packet = json.loads(messages[-1]["content"])
        assert packet["question"] == "А какая у него чистота?"
        assert packet["dialogue_context"][-1]["text"].strip() == "Обсуждали чистоту учебного газа."
        assert "e999" not in json.dumps(packet)
        assert packet["documents"][0]["result"]["warnings"] == ["Synthetic fixture"]
        return {"content": f"Учебный газ: ≥ 99.5 %. [{source_id(packet)}]"}

    result = asyncio.run(
        ConversationAgent(
            provider=writer,
            reviewer=reviewer,
            session_factory=large_session,
            context_limit=21000,
            output_reserve=512,
            tool_result_bytes=5000,
        ).respond("А какая у него чистота?", history)
    )
    assert len(requests) == 2
    assert result["verification"]["status"] == "reviewed"
    assert result["context"]["compactions"] >= 1
    assert result["context"]["estimated_input_tokens"] + 512 <= 21000
    assert history == original and result["summary_update"] is None
    assert result["events"][0]["result"]["data"]["description"] == "A" * 3900


def test_parallel_reads_after_feedback_keep_revision_when_context_is_rebuilt():
    requests = []

    async def writer(messages, tools):
        requests.append(copy.deepcopy(messages))
        assert estimate(messages, tools) + 512 <= 26000
        if len(requests) == 1:
            return read()
        packet = json.loads(messages[-1]["content"])
        if len(requests) == 2:
            return {"content": f"Учебный газ: ≥ 90 %. [{source_id(packet)}]"}
        assert ANSWER_REVISION_NOTE in messages[0]["content"]
        if len(requests) == 3:
            assert tools
            assert estimate(messages, tools) + 512 + 5000 <= 26000
            calls = [read({"filter": "x" * 4400})["tool_calls"][0] for _ in range(2)]
            calls[1]["id"] = "second"
            return {"content": None, "tool_calls": calls}
        assert tools == []
        assert packet["revision_feedback"]["status"] == "rejected"
        assert packet["previous_answer"].startswith("Учебный газ: ≥ 90 %.")
        return {"content": f"Учебный газ: ≥ 99.5 %. [{source_id(packet)}]"}

    result = asyncio.run(
        ConversationAgent(
            provider=writer,
            reviewer=reviewer,
            session_factory=large_session,
            context_limit=26000,
            output_reserve=512,
            tool_result_bytes=5000,
            max_calls=3,
        ).respond("Прочитай учебные данные", [])
    )
    assert len(requests) == 4 and len(result["events"]) == 3
    assert result["context"]["compactions"] >= 1
    assert result["verification"]["status"] == "reviewed"
    assert result["verification"]["reconciliation"]["revisions"] == 1
