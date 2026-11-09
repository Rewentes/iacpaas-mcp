"""Conversation prose, bounded citation checks and persisted live events; no external APIs."""

import asyncio
import json
from contextlib import asynccontextmanager
from types import SimpleNamespace

import pytest
from starlette.testclient import TestClient

from gas_web.app import create_app
from standalone_agent.answers import authored_answer, visible_text
from standalone_agent.conversation import ConversationAgent


async def accepted_review(messages, tools):
    payload = json.loads(messages[-1]["content"])
    import re

    return {
        "content": json.dumps(
            {
                "answers_question": True,
                "reason": "Соответствует вопросу по сценарию.",
                "blocks": [
                    {
                        "id": block["id"],
                        "status": "supported"
                        if re.findall(r"\[(e\d+)\]", block["text"])
                        else "not_factual",
                        "source_ids": re.findall(r"\[(e\d+)\]", block["text"]),
                        "reason": "Проверка сценарного ответа.",
                    }
                    for block in payload["blocks"]
                ],
            }
        )
    }


def final(text, ids=(), intent="data"):
    return {
        "role": "assistant",
        "content": json.dumps(
            {"type": "answer", "intent": intent, "text": text, "source_ids": list(ids)}
        ),
    }


@asynccontextmanager
async def gas_session():
    class Session:
        async def list_tools(self):
            return SimpleNamespace(
                tools=[
                    SimpleNamespace(
                        name="get_gas_specification",
                        description="Gas",
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
                    "warnings": [],
                    "data": {
                        "path": "/gases",
                        "id": "argon-grade",
                        "gas_name": "Аргон высший сорт",
                        "facts": [
                            {
                                "value_kind": "normative_limit",
                                "concept_id": "argon-grade",
                                "field": "argon",
                                "value": 99.993,
                                "unit": "%",
                                "operator": "≥",
                                "label": "Аргон",
                            }
                        ],
                    },
                },
            )

    yield Session()


def test_authored_prose_and_ordered_progress_arrive_before_tools_and_final():
    seen = []

    async def callback(event):
        seen.append(event)

    async def provider(messages, tools):
        if messages[-1]["role"] != "tool":
            assert seen[0]["kind"] == "status"
            return {
                "role": "assistant",
                "content": "Сверю требования к чистоте аргона.",
                "reasoning": "PRIVATE INTERNAL",
                "tool_calls": [
                    {
                        "id": "gas",
                        "type": "function",
                        "function": {"name": "get_gas_specification", "arguments": "{}"},
                    }
                ],
            }
        assert [x["kind"] for x in seen] == ["status", "commentary", "tool_start", "tool_end"]
        source = next(
            x
            for x in json.loads(messages[-1]["content"])["evidence"]
            if x.get("value_kind") == "normative_limit"
        )
        return final(
            "Для аргона установлена чистота **не менее 99,993 %**. Это нормативная граница, а не измерение. ["
            + source["id"]
            + "]",
            [source["id"]],
        )

    result = asyncio.run(
        ConversationAgent(
            provider=provider, session_factory=gas_session, reviewer=accepted_review
        ).respond("Чистота аргона?", [], on_event=callback)
    )
    assert result["verification"]["status"] == "reviewed"
    assert result["answer"].startswith("Для аргона")
    assert "nodes/" not in result["answer"]
    assert result["verification"]["progress"] == seen
    assert "PRIVATE" not in json.dumps(result)
    assert result["sources"][0]["operator"] == "≥"


@pytest.mark.parametrize("intent", ["guidance", "conversation", "clarification"])
def test_non_data_answers_are_natural_and_do_not_claim_verification(intent):
    async def provider(messages, tools):
        return final(
            "Укажите два газа и их сорта. Затем сравним нормативные ограничения.", intent=intent
        )

    result = asyncio.run(
        ConversationAgent(
            provider=provider, session_factory=gas_session, reviewer=accepted_review
        ).respond("Как можно работать с ресурсами?", [])
    )
    assert result["answer"].startswith("Укажите два газа")
    assert result["verification"]["status"] == "not_applicable"
    assert result["sources"] == [] and result["events"] == []


def evidence():
    row = {
        "id": "e1",
        "resource_path": "/gases",
        "concept_id": "argon",
        "field": "purity",
        "label": "Аргон",
        "value": 99.993,
        "unit": "%",
        "operator": "≥",
        "value_kind": "normative_limit",
    }
    return {"e1": row}, {"e1": {"backend": "iacpaas", "complete": True, "warnings": []}}


@pytest.mark.parametrize(
    "text,ids,status",
    [
        ("Чистота 99.993 % [e1]", ["e1"], "grounded"),
        ("Чистота 100 % [e1]", ["e1"], "rejected"),
        ("Данные [e99]", ["e1"], "error"),
        ("Данные [e99]", ["e99"], "error"),
        ("Не хватает данных.", [], "insufficient_data"),
    ],
)
def test_unknown_sources_and_unconfirmed_numbers_are_never_marked_verified(text, ids, status):
    registry, provenance = evidence()
    result = authored_answer(json.loads(final(text, ids)["content"]), registry, provenance)
    assert result["verification"]["status"] == status
    if status in ("insufficient_data", "rejected"):
        assert "100 %" not in result["answer"]


def test_visible_text_omits_private_blocks_and_redacts_credentials(monkeypatch):
    monkeypatch.setenv("IACPAAS_TOKEN", "private-platform-token-12345")
    text = visible_text(
        "<think>private scratchpad</think>Проверю данные. private-platform-token-12345"
    )
    assert "scratchpad" not in text and "private-platform-token" not in text
    assert "Проверю данные." in text


class StreamingAgent:
    calls = 0

    async def respond(self, message, history, summary=None, *, on_event=None):
        self.calls += 1
        progress = [
            {"kind": "commentary", "text": "Найду нужные характеристики."},
            {"kind": "tool_start", "text": "Читаю ресурс."},
        ]
        for event in progress:
            await on_event(event)
        return {
            "answer": "Связный **ответ**.",
            "sources": [],
            "events": [],
            "summary_update": None,
            "context": {"estimated_input_tokens": 100, "limit": 48000, "compactions": 0},
            "verification": {
                "status": "not_applicable",
                "reason": "Пояснение",
                "progress": progress,
            },
        }


def test_sse_progress_done_storage_and_json_idempotent_replay(tmp_path):
    agent = StreamingAgent()
    with TestClient(create_app(db_path=tmp_path / "stream.db", agent=agent)) as client:
        client.headers["X-CSRF-Token"] = client.get("/api/me").json()["csrf_token"]
        conversation = client.post("/api/conversations", json={"title": "Поток"}).json()["id"]
        uri = f"/api/conversations/{conversation}/messages"
        body = {"content": "Сравни", "request_id": "one-turn"}
        response = client.post(uri, json=body, headers={"Accept": "text/event-stream"})
        assert response.status_code == 200
        assert response.headers["content-type"].startswith("text/event-stream")
        frames = [frame for frame in response.text.split("\n\n") if frame.startswith("event:")]
        assert [f.splitlines()[0] for f in frames] == [
            "event: progress",
            "event: progress",
            "event: done",
        ]
        done = json.loads(frames[-1].split("data: ", 1)[1])
        assert done["status"] == 200
        assert (
            done["result"]["assistant_message"]["verification"]["progress"][0]["kind"]
            == "commentary"
        )
        replay = client.post(uri, json=body, headers={"Accept": "text/event-stream"})
        assert replay.json() == done["result"] and agent.calls == 1
        history = client.get(uri).json()["messages"]
        assert len(history) == 2 and history[-1]["verification"]["progress"]
        assert client.get("/markdown.js").status_code == 200


def test_sse_errors_are_safe_persisted_and_not_reexecuted(tmp_path):
    class Broken(StreamingAgent):
        async def respond(self, *args, on_event=None, **kwargs):
            self.calls += 1
            await on_event({"kind": "status", "text": "Проверяю доступность."})
            raise RuntimeError("PRIVATE CREDENTIAL")

    agent = Broken()
    with TestClient(create_app(db_path=tmp_path / "failure.db", agent=agent)) as client:
        client.headers["X-CSRF-Token"] = client.get("/api/me").json()["csrf_token"]
        cid = client.post("/api/conversations", json={"title": "Ошибка"}).json()["id"]
        uri = f"/api/conversations/{cid}/messages"
        body = {"content": "Вопрос", "request_id": "failure"}
        response = client.post(uri, json=body, headers={"Accept": "text/event-stream"})
        assert "PRIVATE" not in response.text and "event: progress" in response.text
        frame = response.text.split("event: done\ndata: ")[1].strip()
        done = json.loads(frame)
        assert done["status"] == 502 and not done["result"]["error"]["retryable"]
        assert client.post(uri, json=body).status_code == 502 and agent.calls == 1
        assert client.get(uri).json()["messages"][0]["state"] == "failed"


@pytest.mark.parametrize(
    "content",
    [
        "Для сравнения укажите **два газа и их сорта**. Затем сопоставим чистоту и примеси.",
        "<think>private scratchpad</think>Какие два газа сравним?",
        '{"type":"answer","text":"Укажите два газа.","source_ids":[]}',
        '{"answer":"Укажите два газа."}',
    ],
)
def test_plain_prose_and_small_envelope_variations_do_not_break_conversation(content):
    async def provider(messages, tools):
        return {"role": "assistant", "content": content}

    result = asyncio.run(
        ConversationAgent(
            provider=provider, session_factory=gas_session, reviewer=accepted_review
        ).respond("Объясни возможности помощника.", [])
    )
    assert result["verification"]["status"] == "not_applicable"
    assert result["answer"] and "scratchpad" not in result["answer"]
    assert not result["events"]


def test_plain_factual_prose_still_checks_current_evidence_and_numbers():
    async def provider(messages, tools):
        if messages[-1]["role"] != "tool":
            return {
                "content": "Прочитаю нормативные ограничения.",
                "tool_calls": [
                    {
                        "id": "gas",
                        "type": "function",
                        "function": {"name": "get_gas_specification", "arguments": "{}"},
                    }
                ],
            }
        source = next(
            x
            for x in json.loads(messages[-1]["content"])["evidence"]
            if x.get("value_kind") == "normative_limit"
        )
        return {"content": "Для аргона: **не менее 99,993 %**. [" + source["id"] + "]"}

    result = asyncio.run(
        ConversationAgent(
            provider=provider, session_factory=gas_session, reviewer=accepted_review
        ).respond("Чистота?", [])
    )
    assert result["verification"]["status"] == "reviewed"
    assert result["sources"][0]["value"] == 99.993


def test_unknown_citations_in_plain_prose_are_rejected():
    async def provider(messages, tools):
        return {"content": "Чистота 100 % [e999]"}

    result = asyncio.run(
        ConversationAgent(
            provider=provider, session_factory=gas_session, reviewer=accepted_review
        ).respond("Чистота?", [])
    )
    assert result["verification"]["status"] == "error"
    assert result["sources"] == [] and "100" not in result["answer"]
