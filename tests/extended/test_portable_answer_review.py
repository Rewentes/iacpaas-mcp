"""Semantic review gates and deterministic bound checks, entirely offline."""

import asyncio
import copy
import json

import pytest

from standalone_agent.answers import authored_answer, visible_text
from standalone_agent.conversation import ConversationAgent
from standalone_agent.verification import (
    answer_blocks,
    normative_issues,
    review_messages,
    validate_review,
)

SOURCE = {
    "id": "e1",
    "resource_path": "/gases",
    "concept_id": "ar-purity",
    "field": "purity",
    "label": "Аргон высший сорт · Ar",
    "value": 99.993,
    "unit": "%",
    "operator": "≥",
    "value_kind": "normative_limit",
}
PROVENANCE = {
    "e1": {
        "backend": "iacpaas",
        "complete": True,
        "warnings": [],
        "data": {"gas_name": "Аргон высший сорт", "standard": "ГОСТ", "path": "/gases"},
    }
}
GOOD = "Для аргона нормативная чистота **не менее 99,993 %**. [e1]"


def report_for(blocks, status="supported", relevant=True):
    return {
        "content": json.dumps(
            {
                "answers_question": relevant,
                "reason": "Проверено соответствие вопросу.",
                "blocks": [
                    {
                        "id": block["id"],
                        "status": status,
                        "source_ids": [] if status == "not_factual" else ["e1"],
                        "reason": "Основание в указанном источнике.",
                    }
                    for block in blocks
                ],
            }
        )
    }


@pytest.mark.parametrize(
    "text,code",
    [
        (GOOD, None),
        ("Чистота аргона не более 99,993 %. [e1]", "bound_mismatch"),
        ("Чистота аргона не менее 99,993 ppm. [e1]", "unit_mismatch"),
        ("Чистота аргона 99,993 %. [e1]", "bound_mismatch"),
        ("Чистота аргона ≥ 99.993 %. [e1]", None),
    ],
)
def test_normative_value_needs_the_matching_unit_and_bound(text, code):
    issues = normative_issues(answer_blocks(text), [SOURCE])
    assert (issues[0]["code"] if issues else None) == code


def table_source(source_id, value, operator="≥"):
    source = copy.deepcopy(SOURCE)
    source.update(id=source_id, value=value, operator=operator)
    return source


def test_table_row_label_bound_applies_to_each_value_cell():
    sources = [table_source("e1", 99.993), table_source("e2", 99.9999)]
    table = "| He, ≥ | 99,993 % [e1] | 99,9999 % [e2] |"
    assert normative_issues(answer_blocks(table), sources) == []


def test_table_row_label_bound_is_still_checked():
    sources = [table_source("e1", 99.993), table_source("e2", 99.9999)]
    table = "| He, ≤ | 99,993 % [e1] | 99,9999 % [e2] |"
    issues = normative_issues(answer_blocks(table), sources)
    assert [(item["source_id"], item["code"]) for item in issues] == [
        ("e1", "bound_mismatch"),
        ("e2", "bound_mismatch"),
    ]


def test_explicit_table_cell_bound_overrides_the_row_label():
    sources = [table_source("e1", 99.993), table_source("e2", 99.9999)]
    table = "| He, ≥ | 99,993 % [e1] | ≤ 99,9999 % [e2] |"
    issues = normative_issues(answer_blocks(table), sources)
    assert [(item["source_id"], item["code"]) for item in issues] == [
        ("e2", "bound_mismatch")
    ]


def test_table_row_label_can_carry_the_unit_for_each_value_cell():
    sources = [table_source("e1", 99.993), table_source("e2", 99.9999)]
    table = "| He, % | ≥ 99,993 [e1] | ≥ 99,9999 [e2] |"
    assert normative_issues(answer_blocks(table), sources) == []


def test_table_row_label_can_carry_both_bound_and_unit():
    sources = [table_source("e1", 99.993), table_source("e2", 99.9999)]
    table = "| He, ≥, % | 99,993 [e1] | 99,9999 [e2] |"
    assert normative_issues(answer_blocks(table), sources) == []


def test_wrong_table_row_label_unit_is_still_checked():
    sources = [table_source("e1", 99.993), table_source("e2", 99.9999)]
    table = "| He, ppm | ≥ 99,993 [e1] | ≥ 99,9999 [e2] |"
    issues = normative_issues(answer_blocks(table), sources)
    assert [(item["source_id"], item["code"]) for item in issues] == [
        ("e1", "unit_mismatch"),
        ("e2", "unit_mismatch"),
    ]


def test_explicit_table_cell_unit_overrides_the_row_label():
    sources = [table_source("e1", 99.993), table_source("e2", 99.9999)]
    table = "| He, % | ≥ 99,993 [e1] | ≥ 99,9999 ppm [e2] |"
    issues = normative_issues(answer_blocks(table), sources)
    assert [(item["source_id"], item["code"]) for item in issues] == [
        ("e2", "unit_mismatch")
    ]


def test_review_payload_keeps_gas_identity_normative_kind_and_source_provenance():
    blocks, messages = review_messages("Чистота?", GOOD, [SOURCE], PROVENANCE, [])
    payload = json.loads(messages[-1]["content"])
    source = payload["sources"][0]
    assert source["context"]["gas_name"] == "Аргон высший сорт"
    assert source["value_kind"] == "normative_limit" and source["operator"] == "≥"
    assert payload["documents"][0]["result"]["backend"] == "iacpaas"
    assert payload["blocks"] == blocks


def test_review_covers_every_block_and_records_exact_text():
    blocks = answer_blocks(GOOD + "\n\nКакой сорт сравним?")
    response = report_for(blocks)
    parsed = json.loads(response["content"])
    parsed["blocks"][1].update(status="not_factual", source_ids=[])
    result = validate_review({"content": json.dumps(parsed)}, blocks, [SOURCE])
    assert result["status"] == "accepted"
    assert result["blocks"][0]["text"] == GOOD


@pytest.mark.parametrize(
    "change",
    ["missing_block", "duplicate_block", "unknown_source", "cited_as_non_factual", "tools"],
)
def test_incomplete_or_forged_reviews_cannot_pass(change):
    blocks = answer_blocks(GOOD + "\n\n" + GOOD)
    response = report_for(blocks)
    data = json.loads(response["content"])
    if change == "missing_block":
        data["blocks"].pop()
    if change == "duplicate_block":
        data["blocks"][1]["id"] = "b1"
    if change == "unknown_source":
        data["blocks"][0]["source_ids"] = ["e99"]
    if change == "cited_as_non_factual":
        data["blocks"][0].update(status="not_factual", source_ids=[])
    if change == "tools":
        response["tool_calls"] = [{"function": {"name": "query_graph"}}]
    response["content"] = json.dumps(data)
    with pytest.raises(ValueError):
        validate_review(response, blocks, [SOURCE])


async def noop(*args):
    pass


def render(text=GOOD):
    return authored_answer(
        {"type": "answer", "intent": "data", "text": text, "source_ids": ["e1"]},
        {"e1": copy.deepcopy(SOURCE)},
        PROVENANCE,
    )


def test_same_number_for_wrong_gas_is_rejected_by_semantic_review():
    # Numeric set matching alone accepts it; semantic review must stop publication.
    draft = render("Для гелия чистота не менее 99,993 %. [e1]")
    assert draft["verification"]["status"] == "grounded"

    async def reviewer(messages, tools):
        payload = json.loads(messages[-1]["content"])
        assert payload["sources"][0]["context"]["gas_name"] == "Аргон высший сорт"
        return report_for(payload["blocks"], status="contradicted")

    result = asyncio.run(
        ConversationAgent(reviewer=reviewer)._review_answer(
            "Чистота гелия?", draft, PROVENANCE, [], noop
        )
    )
    assert result["verification"]["review"]["status"] == "rejected"
    assert result["verification"]["status"] == "rejected"
    assert "99,993" not in result["answer"]


def test_verified_fact_that_does_not_answer_question_is_withheld():
    async def reviewer(messages, tools):
        return report_for(json.loads(messages[-1]["content"])["blocks"], relevant=False)

    result = asyncio.run(
        ConversationAgent(reviewer=reviewer)._review_answer(
            "Какие данные нужны для сравнения?", render(), PROVENANCE, [], noop
        )
    )
    assert result["verification"]["review"]["answers_question"] is False
    assert "99,993" not in result["answer"]


def test_bound_substitution_is_stopped_before_paid_review():
    calls = []

    async def reviewer(*args):
        calls.append(1)
        raise AssertionError

    result = asyncio.run(
        ConversationAgent(reviewer=reviewer)._review_answer(
            "Чистота?", render("Чистота не более 99,993 %. [e1]"), PROVENANCE, [], noop
        )
    )
    assert calls == [] and result["verification"]["review"]["issues"][0]["code"] == "bound_mismatch"


@pytest.mark.parametrize("failure", ["provider", "invalid_report", "context"])
def test_unavailable_review_never_publishes_unchecked_draft(failure):
    calls = []

    async def reviewer(messages, tools):
        calls.append(1)
        if failure == "provider":
            raise RuntimeError("PRIVATE-UPSTREAM-KEY")
        return {"content": "Нельзя считать это корректным отчётом."}

    agent = ConversationAgent(reviewer=reviewer)
    draft = render()
    if failure == "context":
        draft["answer"] += "\n\n" + "Большой фрагмент. " * 5000
    result = asyncio.run(agent._review_answer("Чистота?", draft, PROVENANCE, [], noop))
    assert result["verification"]["review"]["status"] == "unavailable"
    assert "99,993" not in result["answer"] and "PRIVATE" not in json.dumps(result)
    if failure == "context":
        assert calls == []


def test_accepted_review_is_persistable_and_checks_are_explicit(tmp_path):
    from gas_web.storage import Storage

    async def reviewer(messages, tools):
        return report_for(json.loads(messages[-1]["content"])["blocks"])

    result = asyncio.run(
        ConversationAgent(reviewer=reviewer)._review_answer(
            "Чистота аргона?", render(), PROVENANCE, [], noop
        )
    )
    result.update(
        events=[],
        summary_update=None,
        context={"estimated_input_tokens": 100, "limit": 48000, "compactions": 0},
    )
    storage = Storage(tmp_path / "review.db")
    owner, _ = storage.new_owner()
    cid = storage.create_conversation(owner["id"], "Проверка")["id"]
    user, _, _ = storage.start_turn(cid, "one", "Чистота?")
    saved = storage.finish_turn(cid, "one", user, result)["assistant_message"]
    assert saved["verification"]["status"] == "reviewed"
    assert saved["verification"]["checks"]["semantic_blocks"] is True
    assert storage.history(cid)[-1]["verification"]["review"]["blocks"][0]["text"] == GOOD


def test_long_prose_and_review_json_are_not_silently_truncated(monkeypatch):
    monkeypatch.setenv("IACPAAS_TOKEN", "sensitive-cross-boundary-secret")
    text = "Пояснение. " * 600 + "sensitive-cross-boundary-secret"
    clean = visible_text(text)
    assert len(clean) > 2048 and "sensitive-cross-boundary-secret" not in clean
    blocks = answer_blocks(GOOD + "\n\n" + GOOD + "\n\n" + GOOD)
    response = report_for(blocks)
    parsed = json.loads(response["content"])
    for item in parsed["blocks"]:
        item["reason"] = "Подробное основание. " * 30
    response["content"] = json.dumps(parsed, ensure_ascii=False)
    assert len(response["content"]) > 2048
    assert validate_review(response, blocks, [SOURCE])["status"] == "accepted"


def test_legacy_id_selection_cannot_bypass_semantic_review():
    from test_portable_authored_conversation import gas_session

    calls = []

    async def provider(messages, tools):
        calls.append(messages)
        if messages[0]["content"].startswith("Проверь ответ помощника"):
            payload = json.loads(messages[-1]["content"])
            report = report_for(payload["blocks"], relevant=False)
            data = json.loads(report["content"])
            for block in data["blocks"]:
                block["source_ids"] = [payload["sources"][0]["id"]]
            return {"content": json.dumps(data)}
        if messages[-1]["role"] != "tool":
            return {
                "content": None,
                "tool_calls": [
                    {
                        "id": "gas",
                        "type": "function",
                        "function": {"name": "get_gas_specification", "arguments": "{}"},
                    }
                ],
            }
        source = next(
            row
            for row in json.loads(messages[-1]["content"])["evidence"]
            if row.get("value_kind") == "normative_limit"
        )
        return {"content": json.dumps({"source_ids": [source["id"]]})}

    result = asyncio.run(
        ConversationAgent(provider=provider, session_factory=gas_session).respond(
            "Сравни аргон и гелий", []
        )
    )
    assert len(calls) == 3
    assert result["verification"]["review"]["answers_question"] is False
    assert result["verification"]["status"] == "rejected"
    assert "99.993" not in result["answer"]


def test_reviewer_gets_working_summary_without_failed_turns():
    history = [
        {"role": "user", "content": "Неуспешный вопрос", "state": "failed"},
        {"role": "assistant", "content": "Уточнили сорт газа."},
    ]
    _, messages = review_messages(
        "А у него?", GOOD, [SOURCE], PROVENANCE, history, {"text": "Речь об аргоне высшего сорта."}
    )
    payload = json.loads(messages[-1]["content"])
    assert payload["dialogue_summary"] == "Речь об аргоне высшего сорта."
    assert all(item["text"] != "Неуспешный вопрос" for item in payload["dialogue_context"])


def test_supported_parts_are_published_without_the_rejected_claim():
    async def reviewer(messages, tools):
        payload = json.loads(messages[-1]["content"])
        data = json.loads(report_for(payload["blocks"])["content"])
        data["blocks"][1]["status"] = "unsupported"
        return {"content": json.dumps(data)}

    draft = render(GOOD + "\n\nВсего найдено 999 марок. [e1]")
    result = asyncio.run(
        ConversationAgent(reviewer=reviewer)._review_answer(
            "Какая чистота?", draft, PROVENANCE, [], noop
        )
    )
    assert result["verification"]["status"] == "partial"
    assert "99,993" in result["answer"] and "999" not in result["answer"]
    assert "Подтверждена только эта часть" in result["answer"]


def test_warning_alone_is_not_a_useful_partial_data_answer():
    warning = "Оригиналы внешних ссылок автоматически не загружались."
    provenance = copy.deepcopy(PROVENANCE)
    provenance["e1"]["warnings"] = [warning]
    draft = {
        "answer": "Неподтверждённый газ. [e1]\n\n" + warning,
        "sources": [SOURCE],
        "verification": {"status": "grounded", "reason": "Ссылки"},
    }

    async def reviewer(messages, tools):
        payload = json.loads(messages[-1]["content"])
        data = json.loads(report_for(payload["blocks"])["content"])
        data["blocks"][0]["status"] = "unsupported"
        return {"content": json.dumps(data)}

    result = asyncio.run(
        ConversationAgent(reviewer=reviewer)._review_answer(
            "Какие газы?", draft, provenance, [], noop
        )
    )
    assert result["verification"]["status"] == "rejected"
    assert warning not in result["answer"]
