"""Validate citations separately from authored prose; never claim semantic proof."""

import json
import re
from decimal import Decimal

from .citations import citation_ids, strip_citations
from .runtime import redact


def visible_text(text):
    if not isinstance(text, str):
        return ""
    # Some providers put private reasoning in content instead of a separate field.
    text = re.sub(r"<think(?:ing)?>.*?</think(?:ing)?>", "", text, flags=re.S | re.I)
    text = re.split(r"<think(?:ing)?>", text, maxsplit=1, flags=re.I)[0]
    return redact(text, max_text=None).strip()


def numbers(text):
    text = strip_citations(text)
    text = re.sub(r"(?m)^\s*(?:\*\*|__)?\d+(?:\*\*|__)?[.)](?:\*\*|__)?\s+", "", text)
    return {
        Decimal(value.replace(",", "."))
        for value in re.findall(r"(?<![\w])[+-]?\d+(?:[.,]\d+)?(?:[eE][+-]?\d+)?", text)
    }


def unit_key(unit):
    unit = re.sub(r"\s+", "", unit).casefold().replace("м3", "м³")
    return "%" if unit in {"%", "процент", "процента", "процентов", "об.%"} else unit


def quantities(text):
    """Only explicit physical quantities; list indices and bare counts are not units."""
    pattern = r"(?<![\w])[+-]?\d+(?:[.,]\d+)?(?:[eE][+-]?\d+)?"
    units = r"%|ppm|мг\s*/\s*м[³3]|г\s*/\s*м[³3]|кг\s*/\s*м[³3]|МПа|кПа|Па|бар|°[CС]|процентов|процента|процент"
    return [
        (Decimal(match[1].replace(",", ".")), unit_key(match[2]))
        for match in re.finditer(
            r"(" + pattern + r")\s*(?:\*\*)?\s*(" + units + r")(?!\w)", text, re.I
        )
    ]


def parse_answer(content, *, has_evidence=False):
    """Ordinary assistant prose is the default; accept old structured replies too."""
    text = visible_text(content)
    if not text:
        raise ValueError("empty_answer")
    fence = re.fullmatch(r"\s*```(?:json)?\s*\n(.*?)\n```\s*", text, re.DOTALL)
    candidate = fence[1] if fence else text
    try:
        decision = json.loads(candidate)
    except ValueError:
        # A broken machine envelope is not a human answer. Normal Markdown needs
        # no repair call, second model, or extra inference to be displayed.
        if candidate.lstrip().startswith(("{", "[{")):
            raise ValueError("broken_answer_envelope") from None
        decision = None
    if decision is not None:
        if not isinstance(decision, dict):
            raise ValueError("invalid_answer_envelope")
        if "claims" in decision:
            return decision  # legacy field-by-field verification remains strict
        if "text" in decision or "answer" in decision:
            prose = decision.get("text", decision.get("answer"))
            if not isinstance(prose, str):
                raise ValueError("invalid_answer_text")
            ids = decision.get("source_ids", citation_ids(prose))
            return {
                "type": "answer",
                "intent": decision.get("intent", "data" if ids or has_evidence else "conversation"),
                "text": prose,
                "source_ids": ids,
            }
        if "source_ids" in decision:
            return decision
        raise ValueError("unknown_answer_envelope")
    ids = citation_ids(text)
    return {
        "type": "answer",
        "intent": "data" if ids or has_evidence else "conversation",
        "text": text,
        "source_ids": ids,
    }


def authored_answer(decision, registry, provenance):
    """Return prose + selected evidence with an explicitly limited check report."""
    if (
        set(decision) != {"type", "intent", "text", "source_ids"}
        or decision["intent"] not in {"data", "guidance", "conversation", "clarification"}
        or not isinstance(decision["text"], str)
        or not decision["text"].strip()
        or len(decision["text"]) > 30000
        or not isinstance(decision["source_ids"], list)
        or len(decision["source_ids"]) > 64
        or any(not isinstance(key, str) for key in decision["source_ids"])
    ):
        raise ValueError("invalid_answer")
    text = visible_text(decision["text"])
    if not text:
        raise ValueError("invalid_answer")
    ids = list(dict.fromkeys(decision["source_ids"]))
    cited = set(citation_ids(text))
    if any(key not in registry for key in ids) or cited - set(ids):
        return {
            "answer": "Не удалось подтвердить ссылки ответа на источники. Уточните запрос.",
            "sources": [],
            "verification": {
                "status": "error",
                "reason": "Ответ ссылается на неизвестный источник.",
            },
        }
    sources = [registry[key] for key in ids]
    if not ids:
        return {
            "answer": text,
            "sources": [],
            "verification": {
                "status": "insufficient_data" if decision["intent"] == "data" else "not_applicable",
                "reason": "Ответ не подтверждён чтением ресурсов в этом ходе."
                if decision["intent"] == "data"
                else "Пояснение или уточнение; факты платформы не проверялись.",
            },
        }
    supported = set()
    supported_quantities = set()
    for source in sources:
        for key in ("value", "label", "unit"):
            supported.update(numbers(str(source.get(key, ""))))
        supported_quantities.update(quantities(str(source.get("value", ""))))
        if type(source.get("value")) in (int, float) and source.get("unit"):
            supported_quantities.add((Decimal(str(source["value"])), unit_key(source["unit"])))
    unmatched_numbers = numbers(text) - supported
    unmatched_quantities = set(quantities(text)) - supported_quantities
    numeric_check = {
        "status": "failed"
        if unmatched_quantities
        else "needs_semantic_review"
        if unmatched_numbers
        else "passed",
        "unmatched_quantities": [
            {"value": str(value), "unit": unit} for value, unit in sorted(unmatched_quantities)
        ],
        "unmatched_numbers": sorted(str(value) for value in unmatched_numbers),
    }
    if unmatched_quantities:
        return {
            "answer": "Подготовленный ответ содержит величины, которые не удалось подтвердить выбранными источниками. Этот вариант не опубликован; полученные источники сохранены ниже.",
            "sources": sources,
            "verification": {
                "status": "rejected",
                "reason": "Проверка обнаружила неподтверждённую величину в ответе агента.",
                "validation": {
                    "code": "quantity_mismatch",
                    "numeric_check": numeric_check,
                    "candidate_text": text,
                },
            },
        }
    warnings = []
    for key in ids:
        context = provenance[key]
        warnings.extend(context.get("warnings", []))
        if context.get("complete") is False:
            warnings.append("Прочитана только часть данных в указанной области запроса.")
    warnings = list(dict.fromkeys(
        visible_text(w) for w in warnings if isinstance(w, str) and visible_text(w)
    ))
    if any(provenance[key].get("backend") == "mock" for key in ids):
        text += "\n\nИспользованы демонстрационные данные MOCK."
    return {
        "answer": text,
        "sources": sources,
        "verification": {
            "status": "grounded",
            "reason": "Ссылки найдены в результатах MCP, числа сопоставлены с выбранными данными. Это не полная проверка смысла, единиц или вывода.",
            "checks": {"source_references": True, "numeric_values": not unmatched_numbers},
            "numeric_check": numeric_check,
            "warnings": warnings,
        },
    }
