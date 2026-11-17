"""Working evidence can fill a fresh turn even when the transcript is short."""

import asyncio
import copy
import json
import sqlite3
from contextlib import asynccontextmanager
from types import SimpleNamespace

from starlette.testclient import TestClient
from test_portable_reconciliation import review_report

from gas_web.app import create_app
from standalone_agent.conversation import ConversationAgent, estimate
from standalone_agent.prompts import ANSWER_REVISION_NOTE
from standalone_agent.working_memory import working_summary_prompt


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
                        "api_key": "fixture-secret-value",
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
    summaries = []
    evidence = None

    async def writer(messages, tools):
        nonlocal evidence
        if messages[0]["content"] == working_summary_prompt(2048):
            assert tools == []
            summaries.append(json.loads(messages[-1]["content"]))
            return {"content": "Прочитана карточка учебного газа; нормативная чистота ≥ 99.5 %."}
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
        assert tools  # Compression preserves the ability to keep investigating.
        assert packet["working_summary"]["text"]
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
    assert len(summaries) == 1
    assert result["events"][0]["result"]["data"]["description"] == "A" * 3900


def test_large_call_envelope_compacts_evidence_and_keeps_followup_context():
    requests = []
    history = [
        {"id": "u0", "role": "user", "content": "Расскажи об учебном газе"},
        {"id": "a0", "role": "assistant", "content": "Обсуждали чистоту учебного газа. [e999]"},
    ]
    original = copy.deepcopy(history)

    async def writer(messages, tools):
        if messages[0]["content"] == working_summary_prompt(2048):
            assert tools == []
            return {
                "content": "Найдена карточка учебного газа; сведения прочитаны, проверку можно продолжить."
            }
        requests.append(copy.deepcopy(messages))
        assert estimate(messages, tools) + 512 <= 21000
        if len(requests) == 1:
            return read({"filter": "x" * 4400})
        assert tools
        packet = json.loads(messages[-1]["content"])
        assert packet["question"] == "А какая у него чистота?"
        assert packet["dialogue_context"][-1]["text"].strip() == "Обсуждали чистоту учебного газа."
        assert "e999" not in json.dumps(packet)
        assert packet["documents"][0]["result"]["warnings"] == ["Synthetic fixture"]
        assert packet["working_summary"]["covered_events"] == 1
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
    summaries = []

    async def writer(messages, tools):
        if messages[0]["content"] == working_summary_prompt(2048):
            assert tools == []
            summaries.append(json.loads(messages[-1]["content"]))
            return {
                "content": "Прочитаны характеристики учебного газа. Чистота ≥ 99.5 %. Проверить ответ."
            }
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
    assert len(summaries) >= 2
    assert summaries[1]["previous_working_summary"]
    assert len(summaries[1]["tool_events"]) == 2
    assert result["context"]["working_summary"]["covered_events"] == 3


def test_web_turn_continues_reading_after_summary_and_keeps_original_history(tmp_path):
    reads, summaries = [], []

    async def writer(messages, tools):
        if messages[0]["content"] == working_summary_prompt(2048):
            assert tools == []
            summaries.append(json.loads(messages[-1]["content"]))
            return {"content": "Учебный газ найден. Проверка свойств продолжается."}
        reads.append(copy.deepcopy(messages))
        if len(reads) == 1:
            return read({"filter": "x" * 4400})
        packet = json.loads(messages[-1]["content"])
        assert packet["working_summary"]["text"]
        if len(reads) == 2:
            assert tools and "Осталось вызовов MCP: 2" in messages[0]["content"]
            return read({"field": "purity"})
        return {"content": f"Учебный газ: ≥ 99.5 %. [{source_id(packet)}]"}

    agent = ConversationAgent(
        provider=writer,
        reviewer=reviewer,
        session_factory=large_session,
        context_limit=21000,
        output_reserve=512,
        tool_result_bytes=5000,
        max_calls=3,
    )
    path = tmp_path / "history.db"
    with TestClient(create_app(db_path=path, agent=agent)) as client:
        client.headers["X-CSRF-Token"] = client.get("/api/me").json()["csrf_token"]
        chat = client.post("/api/conversations", json={"title": "Сжатие"}).json()["id"]
        uri = f"/api/conversations/{chat}/messages"
        question = "Проверь свойства учебного газа"
        response = client.post(uri, json={"request_id": "q1", "content": question})
        assert response.status_code == 200
        history = client.get(uri).json()["messages"]
        assert len(history) == 2 and history[0]["content"] == question
        assert history[1]["content"].startswith("Учебный газ: ≥ 99.5 %.")
        assert "Проверка свойств продолжается" not in history[1]["content"]
    with sqlite3.connect(path) as db:
        context = json.loads(db.execute("SELECT context FROM conversations").fetchone()[0])
        events = [json.loads(row[0]) for row in db.execute("SELECT event FROM events")]
    assert context["working_summary"]["text"] and context["working_summary"]["covered_events"] == 2
    assert len(reads) == 3 and len(summaries) == 2 and len(events) == 2
    assert all(e["result"]["data"]["description"] == "A" * 3900 for e in events)
    assert "fixture-secret-value" not in json.dumps(events + summaries)


def test_inaccurate_working_summary_cannot_override_exact_numeric_evidence():
    requests = []

    async def writer(messages, tools):
        if messages[0]["content"] == working_summary_prompt(2048):
            return {"content": "Чистота учебного газа ≥ 90 %."}  # Deliberately inaccurate.
        requests.append(copy.deepcopy(messages))
        if len(requests) == 1:
            return read({"filter": "x" * 4400})
        packet = json.loads(messages[-1]["content"])
        source = source_id(packet)
        if len(requests) == 2:
            return {"content": f"Учебный газ: ≥ 90 %. [{source}]"}
        assert packet["revision_feedback"]["status"] == "rejected"
        assert next(s for s in packet["sources"] if s["id"] == source)["value"] == 99.5
        return {"content": f"Учебный газ: ≥ 99.5 %. [{source}]"}

    result = asyncio.run(
        ConversationAgent(
            provider=writer,
            reviewer=reviewer,
            session_factory=large_session,
            context_limit=21000,
            output_reserve=512,
            tool_result_bytes=5000,
        ).respond("Чистота учебного газа?", [])
    )
    assert result["verification"]["status"] == "reviewed"
    assert result["verification"]["reconciliation"]["revisions"] == 1
    assert "90 %" not in result["answer"] and "99.5 %" in result["answer"]
