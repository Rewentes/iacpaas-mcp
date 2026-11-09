"""Citation ranges are evidence references, not silently discarded prose."""

import json

import pytest

from standalone_agent.answers import authored_answer, numbers, parse_answer
from standalone_agent.citations import citation_ids
from standalone_agent.verification import review_messages, validate_review


@pytest.mark.parametrize(
    "text,expected",
    [
        ("Список [e68–e70]", ["e68", "e69", "e70"]),
        ("Список [e2-e4][e3]", ["e2", "e3", "e4"]),
        ("Список [e2—4; e6, e8–e9]", ["e2", "e3", "e4", "e6", "e8", "e9"]),
        ("Формат `[e1–e3]` и источник [e4]", ["e4"]),
    ],
)
def test_reference_syntax_is_expanded_and_deduplicated(text, expected):
    assert citation_ids(text) == expected
    assert parse_answer(text)["source_ids"] == expected
    assert numbers(text) == set()


@pytest.mark.parametrize("text", ["[e5-e2]", "[e0]", "[e1-e100000000]", "[e2 wrong]"])
def test_ranges_are_bounded_and_malformed_references_are_rejected(text):
    with pytest.raises(ValueError):
        citation_ids(text)


def test_range_must_not_hide_unknown_source():
    registry = {
        "e1": {
            "id": "e1",
            "resource_path": "/gas",
            "concept_id": "a",
            "field": "name",
            "value": "Аргон",
            "unit": None,
            "operator": None,
        }
    }
    result = authored_answer(parse_answer("Газы [e1–e2]"), registry, {"e1": {"backend": "iacpaas"}})
    assert result["verification"]["status"] == "error" and result["sources"] == []


def test_full_results_are_deduplicated_and_keep_counts_scope_and_all_nodes():
    data = {
        "path": "/gases",
        "total_matches": 66,
        "nodes": [{"id": str(i), "name": f"Газ {i}"} for i in range(60)],
        "complete": False,
    }
    original = {
        "ok": True,
        "backend": "iacpaas",
        "complete": False,
        "data": data,
        "warnings": ["Прочитана часть результата"],
    }
    sources = [
        {
            "id": "e1",
            "field": "path",
            "resource_path": "/gases",
            "concept_id": None,
            "value": "/gases",
            "unit": None,
            "operator": None,
        },
        {
            "id": "e2",
            "field": "nodes/0/name",
            "resource_path": "/gases",
            "concept_id": "0",
            "value": "Газ 0",
            "unit": None,
            "operator": None,
        },
    ]
    # Equal results loaded from JSON are distinct objects but one document.
    provenance = {s["id"]: json.loads(json.dumps(original)) for s in sources}
    blocks, request = review_messages(
        "Какие газы?", "Найдено 66, прочитана часть. [e1–e2]", sources, provenance, []
    )
    payload = json.loads(request[-1]["content"])
    assert len(payload["sources"]) == 2 and len(payload["documents"]) == 1
    result = payload["documents"][0]["result"]
    assert len(result["data"]["nodes"]) == 60
    assert result["data"]["total_matches"] == 66 and result["complete"] is False
    response = {
        "content": json.dumps(
            {
                "answers_question": True,
                "reason": "Данные соответствуют вопросу.",
                "blocks": [
                    {
                        "id": blocks[0]["id"],
                        "status": "supported",
                        "source_ids": ["e2"],
                        "reason": "Сведения в полном документе.",
                    }
                ],
            }
        )
    }
    report = validate_review(
        response, blocks, sources, {s["id"]: s["document_id"] for s in payload["sources"]}
    )
    assert report["status"] == "accepted"


def test_one_document_cannot_cover_another_document():
    sources = [{"id": "e1"}, {"id": "e2"}]
    blocks = [{"id": "b1", "text": "Данные [e1–e2]"}]
    response = {
        "content": json.dumps(
            {
                "answers_question": True,
                "reason": "Ответ",
                "blocks": [
                    {"id": "b1", "status": "supported", "source_ids": ["e1"], "reason": "Проверено"}
                ],
            }
        )
    }
    with pytest.raises(ValueError):
        validate_review(response, blocks, sources, {"e1": "d1", "e2": "d2"})
