import asyncio
from contextlib import asynccontextmanager
from types import SimpleNamespace

import pytest
from portable_review_fixture import accepted_review
from starlette.testclient import TestClient

from gas_web.app import create_app
from standalone_agent.conversation import ConversationAgent, ConversationError
from standalone_agent.working_memory import working_summary_prompt


@pytest.mark.parametrize("code", ["tool_result_limit", "context_limit", "model_output_error"])
def test_known_agent_failure_is_actionable_safe_persistent_and_not_retried(tmp_path, code):
    class Agent:
        calls = 0

        async def respond(self, *args, **kwargs):
            self.calls += 1
            raise ConversationError("SECRET provider response", code)

    agent = Agent()
    with TestClient(create_app(db_path=tmp_path / "history.sqlite3", agent=agent)) as client:
        client.headers["X-CSRF-Token"] = client.get("/api/me").json()["csrf_token"]
        conversation = client.post("/api/conversations", json={"title": "Проверка"}).json()["id"]
        uri = f"/api/conversations/{conversation}/messages"
        body = {"content": "Сравнить два газа", "request_id": "failure"}
        response = client.post(uri, json=body)
        assert response.status_code == 502
        assert response.json()["error"]["code"] == code
        assert "SECRET" not in response.text
        assert "сохранено" in response.json()["error"]["message"]
        assert client.post(uri, json=body).json() == response.json()
        assert agent.calls == 1
        history = client.get(uri).json()["messages"]
        assert len(history) == 1 and history[0]["state"] == "failed"


@asynccontextmanager
async def session():
    class Session:
        async def list_tools(self):
            return SimpleNamespace(tools=[])

    yield Session()


@pytest.mark.parametrize(
    "content,valid", [('```json\n{"claims": []}\n```', True), ('{"type":', False)]
)
def test_final_format_has_precise_diagnostic_and_accepts_whole_json_fence(content, valid):
    async def provider(messages, tools):
        return {"role": "assistant", "content": content}

    agent = ConversationAgent(reviewer=accepted_review, provider=provider, session_factory=session)
    if valid:
        result = asyncio.run(agent.respond("Вопрос", []))
        assert result["verification"]["status"] == "insufficient_data"
    else:
        with pytest.raises(ConversationError) as exc:
            asyncio.run(agent.respond("Вопрос", []))
        assert exc.value.code == "model_output_error"


def test_mcp_task_group_preserves_safe_context_failure():
    @asynccontextmanager
    async def grouped():
        async with asyncio.TaskGroup():
            async with session() as value:
                yield value

    async def provider(messages, tools):
        raise ConversationError("Лимит контекста", "context_limit")

    with pytest.raises(ConversationError) as exc:
        asyncio.run(
            ConversationAgent(
                reviewer=accepted_review, provider=provider, session_factory=grouped
            ).respond("Вопрос", [])
        )
    assert exc.value.code == "context_limit"


def test_native_tool_context_retires_whole_results_but_keeps_full_events_and_evidence():
    import json

    from standalone_agent.conversation import estimate

    calls = []
    body = {
        "ok": True,
        "backend": "iacpaas",
        "data": {"path": "/gases", "value": 42, "description": "А" * 800},
    }

    @asynccontextmanager
    async def native_session():
        class Session:
            async def list_tools(self):
                return SimpleNamespace(
                    tools=[
                        SimpleNamespace(
                            name="read",
                            description="",
                            inputSchema={"type": "object"},
                            annotations=SimpleNamespace(readOnlyHint=True),
                        )
                    ]
                )

            async def call_tool(self, name, arguments):
                return SimpleNamespace(structuredContent=body, isError=False)

        yield Session()

    async def provider(messages, tools):
        if messages[0]["content"] == working_summary_prompt(256):
            assert tools == []
            return {"content": "Прочитаны учебные данные ресурса /gases, поле value. Продолжить проверку."}
        calls.append(json.loads(json.dumps(messages)))
        assert estimate(messages, tools) + 256 <= 9000
        if len(calls) <= 5:
            assert tools
            return {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {
                        "id": str(len(calls)),
                        "type": "function",
                        "function": {"name": "read", "arguments": "{}"},
                    }
                ],
            }
        assert tools == []
        assert "Осталось вызовов MCP: 0" in messages[0]["content"]
        packet = json.loads(messages[-1]["content"])
        evidence = packet.get("evidence", packet.get("sources", []))
        source = next(s for s in evidence if s["field"] == "value")
        return {"role": "assistant", "content": json.dumps({"source_ids": [source["id"]]})}

    agent = ConversationAgent(
        reviewer=accepted_review,
        provider=provider,
        session_factory=native_session,
        context_limit=9000,
        output_reserve=256,
        tool_result_bytes=3000,
        summary_bytes=256,
        max_calls=5,
    )
    result = asyncio.run(agent.respond("Чтения ресурсов", []))
    assert result["verification"]["status"] == "verified"
    assert result["context"]["compactions"] > 0
    assert len(result["events"]) == 5
    assert all(event["result"] == body for event in result["events"])
    assert sum(m["role"] == "tool" for m in calls[-1]) < len(result["events"])
    for dialogue in calls:
        expected = [call["id"] for message in dialogue for call in message.get("tool_calls", [])]
        actual = [message["tool_call_id"] for message in dialogue if message["role"] == "tool"]
        assert actual == expected
