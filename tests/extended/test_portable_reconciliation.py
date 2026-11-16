"""Author and reviewer exchange evidence-backed feedback without losing the conversation."""

import asyncio
import json

import pytest
from starlette.testclient import TestClient
from test_portable_authored_conversation import accepted_review, gas_session

from gas_web.app import create_app
from standalone_agent.conversation import ConversationAgent


def read(call_id="read"):
    return {
        "content": "Проверю карточку.",
        "tool_calls": [
            {
                "id": call_id,
                "type": "function",
                "function": {"name": "get_gas_specification", "arguments": "{}"},
            }
        ],
    }


def normative_id(message):
    return next(
        s["id"]
        for s in json.loads(message["content"])["evidence"]
        if s.get("value_kind") == "normative_limit"
    )


def review_report(messages, status="supported", relevant=True, reason="Есть подтверждение."):
    payload = json.loads(messages[-1]["content"])
    import re

    return {
        "content": json.dumps(
            {
                "answers_question": relevant,
                "reason": reason,
                "blocks": [
                    {
                        "id": b["id"],
                        "status": status,
                        "source_ids": re.findall(r"\[(e\d+)\]", b["text"])
                        if status != "not_factual"
                        else [],
                        "reason": reason,
                    }
                    for b in payload["blocks"]
                ],
            }
        )
    }


def test_semantic_feedback_and_prior_answer_reach_writer_then_reviewer_reconsiders():
    generated, checked = [], []
    topic = "Сравнивали аргон высшего сорта с гелием."
    history = [
        {"id": "u0", "role": "user", "content": "Расскажи об аргоне"},
        {"id": "a0", "role": "assistant", "content": topic + " [e999]"},
    ]
    source = None

    async def writer(messages, tools):
        nonlocal source
        generated.append(messages)
        if len(generated) == 1:
            assert topic in messages[-2]["content"] and "[e999]" not in messages[-2]["content"]
            return read()
        if len(generated) == 2:
            source = normative_id(messages[-1])
            return {"content": f"Аргон подходит для любой задачи. [{source}]"}
        payload = json.loads(messages[-1]["content"])
        assert payload["question"] == "А какая у него чистота?"
        assert payload["dialogue_context"][-1]["text"].strip() == topic
        assert payload["previous_answer"] == f"Аргон подходит для любой задачи. [{source}]"
        assert (
            payload["revision_feedback"]["review"]["reason"] == "Нужна чистота, а не пригодность."
        )
        assert payload["sources"] and payload["documents"]
        return {"content": f"Для аргона высшего сорта нормативная чистота ≥ 99.993 %. [{source}]"}

    async def reviewer(messages, tools):
        payload = json.loads(messages[-1]["content"])
        checked.append(payload)
        if len(checked) == 1:
            return review_report(messages, "unsupported", False, "Нужна чистота, а не пригодность.")
        assert payload["previous_review"]["reason"] == "Нужна чистота, а не пригодность."
        return review_report(messages)

    result = asyncio.run(
        ConversationAgent(provider=writer, reviewer=reviewer, session_factory=gas_session).respond(
            "А какая у него чистота?", history
        )
    )
    assert result["verification"]["status"] == "reviewed"
    assert "99.993" in result["answer"] and "любой задачи" not in result["answer"]
    assert len(generated) == 3 and len(checked) == 2 and len(result["events"]) == 1
    assert result["verification"]["reconciliation"]["resolved"] is True
    assert len(result["verification"]["reconciliation"]["rounds"]) == 2
    assert history[-1]["content"].endswith("[e999]")  # User transcript was not changed.


def test_wrong_reviewer_can_change_verdict_without_writer_changing_correct_fact():
    generated, checks = [], []
    source = None

    async def writer(messages, tools):
        nonlocal source
        generated.append(messages)
        if len(generated) == 1:
            return read()
        if len(generated) == 2:
            source = normative_id(messages[-1])
        else:
            assert json.loads(messages[-1]["content"])["documents"]
        return {"content": f"Аргон: ≥ 99.993 %. [{source}]"}

    async def reviewer(messages, tools):
        payload = json.loads(messages[-1]["content"])
        checks.append(payload)
        if len(checks) == 1:
            return review_report(messages, "unsupported", True, "Не заметил значение в источнике.")
        assert payload["previous_review"]["status"] == "rejected"
        return review_report(messages)

    result = asyncio.run(
        ConversationAgent(provider=writer, reviewer=reviewer, session_factory=gas_session).respond(
            "Чистота аргона?", []
        )
    )
    assert result["verification"]["status"] == "reviewed"
    assert len(checks) == 2 and len(generated) == 3
    assert result["answer"].startswith("Аргон: ≥ 99.993")


def test_bound_feedback_can_request_another_read_but_keeps_original_tool_budget():
    generated = []
    source = None

    async def writer(messages, tools):
        nonlocal source
        generated.append(messages)
        if len(generated) == 1:
            return read("first")
        if len(generated) == 2:
            source = normative_id(messages[-1])
            return {"content": f"Аргон: ≤ 99.993 %. [{source}]"}
        if len(generated) == 3:
            feedback = json.loads(messages[-1]["content"])["revision_feedback"]
            assert feedback["review"]["issues"][0]["code"] == "bound_mismatch"
            assert tools
            return read("second")
        assert tools == []
        source = normative_id(messages[-1])
        return {"content": f"Аргон: ≥ 99.993 %. [{source}]"}

    result = asyncio.run(
        ConversationAgent(
            provider=writer, reviewer=accepted_review, session_factory=gas_session, max_calls=2
        ).respond("Чистота аргона?", [])
    )
    assert result["verification"]["status"] == "reviewed" and len(result["events"]) == 2
    assert result["verification"]["reconciliation"]["revisions"] == 1


def test_persistent_unsupported_number_is_bounded_and_cannot_become_verified():
    calls = []
    source = None

    async def writer(messages, tools):
        nonlocal source
        calls.append(messages)
        if len(calls) == 1:
            return read()
        if len(calls) == 2:
            source = normative_id(messages[-1])
        else:
            packet = json.loads(messages[-1]["content"])
            assert bool(packet["final_revision"]) == (len(calls) == 4)
        return {"content": f"Аргон: ≥ 95.123 %. [{source}]"}

    result = asyncio.run(
        ConversationAgent(
            provider=writer, reviewer=accepted_review, session_factory=gas_session
        ).respond("Чистота?", [])
    )
    assert len(calls) == 4 and len(result["events"]) == 1
    assert result["verification"]["status"] == "rejected"
    assert "95.123" not in result["answer"]
    assert result["verification"]["reconciliation"]["resolved"] is False


def test_model_can_finish_with_a_specific_clarification_after_factual_disagreement():
    calls = []
    source = None

    async def writer(messages, tools):
        nonlocal source
        calls.append(messages)
        if len(calls) == 1:
            return read()
        if len(calls) == 2:
            source = normative_id(messages[-1])
            return {"content": f"Для выбранного сорта предел ≥ 95 %. [{source}]"}
        return {"content": "Какой сорт второго газа нужно сопоставить с выбранным аргоном?"}

    result = asyncio.run(
        ConversationAgent(
            provider=writer, reviewer=accepted_review, session_factory=gas_session
        ).respond("Сравни со вторым газом", [])
    )
    assert result["answer"].startswith("Какой сорт второго газа")
    assert result["verification"]["status"] == "not_applicable"
    assert len(calls) == 3


def test_followup_questions_keep_full_web_history_and_get_different_answers(tmp_path):
    seen = []

    async def writer(messages, tools):
        seen.append(messages)
        last = messages[-1]
        if last["role"] == "tool":
            return {"content": f"Для аргона чистота ≥ 99.993 %. [{normative_id(last)}]"}
        if last["content"] == "Что означает знак из прошлого ответа?":
            previous = [m["content"] for m in messages if m["role"] == "assistant"][-1]
            assert "аргона" in previous and "≥" in previous and "[e" not in previous
            return {"content": "Знак ≥ означает «не менее»: это нижняя нормативная граница."}
        return read()

    agent = ConversationAgent(
        provider=writer, reviewer=accepted_review, session_factory=gas_session
    )
    with TestClient(create_app(db_path=tmp_path / "chat.db", agent=agent)) as client:
        client.headers["X-CSRF-Token"] = client.get("/api/me").json()["csrf_token"]
        chat = client.post("/api/conversations", json={"title": "Уточнения"}).json()["id"]
        uri = f"/api/conversations/{chat}/messages"
        first = client.post(uri, json={"request_id": "q1", "content": "Какая чистота аргона?"})
        second = client.post(
            uri, json={"request_id": "q2", "content": "Что означает знак из прошлого ответа?"}
        )
        assert first.status_code == second.status_code == 200
        history = client.get(uri).json()["messages"]
        assert len(history) == 4
        assert history[1]["content"] != history[3]["content"]
        assert "[e" in history[1]["content"] and "не менее" in history[3]["content"]
    assert len(seen) == 3


@pytest.mark.parametrize("value", [-1, 4, True, 1.5])
def test_revision_budget_configuration_is_bounded(value):
    with pytest.raises(ValueError):
        ConversationAgent(max_answer_revisions=value)


def test_summary_and_recent_answer_survive_revision_without_duplicate_covered_history():
    history = [
        {"id": "covered", "role": "assistant", "content": "Старый длинный обзор. [e500]"},
        {"id": "recent-user", "role": "user", "content": "Теперь аргон высшего сорта"},
        {
            "id": "recent-answer",
            "role": "assistant",
            "content": "Выбрали аргон высшего сорта. [e701]",
        },
    ]
    summary = {
        "text": "Обсуждаем аргон высшего сорта. [e500]",
        "covered_through_message_id": "covered",
    }
    calls = []
    source = None

    async def writer(messages, tools):
        nonlocal source
        calls.append(messages)
        if len(calls) == 1:
            return read()
        if len(calls) == 2:
            source = normative_id(messages[-1])
            return {"content": f"Аргон: ≤ 99.993 %. [{source}]"}
        packet = json.loads(messages[-1]["content"])
        assert packet["dialogue_summary"].strip() == "Обсуждаем аргон высшего сорта."
        assert len(packet["dialogue_context"]) == 2
        assert packet["dialogue_context"][-1]["text"].strip() == "Выбрали аргон высшего сорта."
        return {"content": f"Аргон: ≥ 99.993 %. [{source}]"}

    result = asyncio.run(
        ConversationAgent(
            provider=writer, reviewer=accepted_review, session_factory=gas_session
        ).respond("А какое у него ограничение?", history, summary)
    )
    assert result["verification"]["status"] == "reviewed"
    assert history[0]["content"].endswith("[e500]") and summary["text"].endswith("[e500]")


def test_useful_verified_partial_answer_survives_worse_revisions():
    calls = []
    source = None

    async def writer(messages, tools):
        nonlocal source
        calls.append(messages)
        if len(calls) == 1:
            return read()
        if len(calls) == 2:
            source = normative_id(messages[-1])
            return {
                "content": f"Аргон: ≥ 99.993 %. [{source}]\n\nПодходит для любой задачи. [{source}]"
            }
        return {"content": f"Аргон: ≥ 95.123 %. [{source}]"}

    async def reviewer(messages, tools):
        report = review_report(messages)
        parsed = json.loads(report["content"])
        parsed["blocks"][1]["status"] = "unsupported"
        report["content"] = json.dumps(parsed)
        return report

    result = asyncio.run(
        ConversationAgent(provider=writer, reviewer=reviewer, session_factory=gas_session).respond(
            "Что известно об аргоне?", []
        )
    )
    assert result["verification"]["status"] == "partial"
    assert "99.993" in result["answer"] and "95.123" not in result["answer"]
    assert "любой задачи" not in result["answer"] and len(calls) == 4
    assert result["sources"]


def test_unavailable_reviewer_is_not_misrepresented_as_consensus():
    calls, reviews = [], []
    source = None

    async def writer(messages, tools):
        nonlocal source
        calls.append(messages)
        if len(calls) == 1:
            return read()
        if len(calls) == 2:
            source = normative_id(messages[-1])
        return {"content": f"Аргон: ≥ 99.993 %. [{source}]"}

    async def reviewer(messages, tools):
        reviews.append(messages)
        raise TimeoutError("private provider detail")

    result = asyncio.run(
        ConversationAgent(
            provider=writer, reviewer=reviewer, session_factory=gas_session, max_answer_revisions=1
        ).respond("Чистота?", [])
    )
    assert result["verification"]["status"] == "review_unavailable"
    assert result["verification"]["reconciliation"]["resolved"] is False
    assert len(calls) == 3 and len(reviews) == 2
    assert "private provider detail" not in json.dumps(result)
