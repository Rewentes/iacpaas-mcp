"""Bounded review of every answer block against current evidence, without tools."""

import hashlib
import json
import re
from decimal import Decimal

from .answers import visible_text
from .citations import citation_ids
from .prompts import REVIEW_PROMPT
from .runtime import redact


def answer_blocks(text):
    return [
        {"id": f"b{index + 1}", "text": block.strip()}
        for index, block in enumerate(re.split(r"\n\s*\n|\n(?=\s*[-*]\s)", text))
        if block.strip()
    ]


def source_context(source, result):
    data = result.get("data") or {}
    context = {key: data[key] for key in ("gas_name", "standard") if key in data}
    return context


def _clause_prefix(text, citation_start):
    """Return the text governing a citation.

    In prose a citation belongs to the clause after the previous citation. In a
    Markdown table row the bound may be in the row label, so every value cell in
    that row inherits it instead of starting after the previous cell's citation.
    """
    row_start = text.rfind("\n", 0, citation_start) + 1
    if text[row_start:citation_start].lstrip().startswith("|"):
        return text[row_start:citation_start]
    return re.split(r"\[e\d+\]", text[:citation_start])[-1]


def normative_issues(blocks, sources):
    """Catch local numeric/unit/bound substitutions before the model-based review."""
    issues = []
    number_pattern = r"(?<![\w])[+-]?\d+(?:[.,]\d+)?"
    bound_pattern = r"не\s+более|не\s+менее|не\s+выше|не\s+ниже|≤|≥|<=|>=|максимум|минимум"
    for block in blocks:
        for source in sources:
            if source.get("value_kind") != "normative_limit":
                continue
            citation = "[" + source["id"] + "]"
            if citation not in block["text"]:
                continue
            prefix = _clause_prefix(block["text"], block["text"].index(citation))
            for match in re.finditer(number_pattern, prefix):
                if Decimal(match[0].replace(",", ".")) != Decimal(str(source["value"])):
                    continue
                before, after = prefix[: match.start()], prefix[match.end() :]
                bounds = list(re.finditer(bound_pattern, before, re.I))
                actual = bounds[-1][0].lower() if bounds else ""
                expected = {
                    "≤": {"не более", "не выше", "≤", "<=", "максимум"},
                    "≥": {"не менее", "не ниже", "≥", ">=", "минимум"},
                }
                if re.sub(r"\s+", " ", actual) not in expected.get(source.get("operator"), set()):
                    issues.append(
                        {
                            "block_id": block["id"],
                            "source_id": source["id"],
                            "code": "bound_mismatch",
                        }
                    )
                unit = re.sub(r"\s+", "", source.get("unit") or "").casefold()
                following = re.sub(r"[\s*`_]", "", after).casefold()
                aliases = (
                    ("%", "процент", "об.%", "объемныхпроцент", "объёмныхпроцент")
                    if unit == "%"
                    else (unit,)
                )
                if unit and not following.startswith(aliases):
                    issues.append(
                        {
                            "block_id": block["id"],
                            "source_id": source["id"],
                            "code": "unit_mismatch",
                        }
                    )
    return issues


def review_messages(question, answer, sources, provenance, history, summary=None):
    blocks = answer_blocks(answer)
    documents, keys, references = [], {}, {}
    for source in sources:
        result = redact(provenance[source["id"]], max_text=None, max_list=None)
        key = hashlib.sha256(
            json.dumps(result, ensure_ascii=False, sort_keys=True).encode()
        ).hexdigest()
        if key not in keys:
            keys[key] = f"d{len(documents) + 1}"
            documents.append({"id": keys[key], "result": result})
        references[source["id"]] = keys[key]
    for document in documents:
        document["evidence_ids"] = [
            key for key, value in references.items() if value == document["id"]
        ]
    payload = {
        "question": question,
        "dialogue_summary": summary.get("text", "") if summary else "",
        "dialogue_context": [
            {"role": item["role"], "text": item["content"]}
            for item in [entry for entry in history if entry.get("state") != "failed"][-4:]
        ],
        "blocks": blocks,
        "documents": documents,
        "sources": [
            {
                **{key: value for key, value in source.items() if key != "resource_path"},
                "document_id": references[source["id"]],
                "context": source_context(source, provenance[source["id"]]),
            }
            for source in sources
        ],
    }
    return blocks, [
        {"role": "system", "content": REVIEW_PROMPT},
        {"role": "user", "content": json.dumps(payload, ensure_ascii=False)},
    ]


def validate_review(response, blocks, sources, document_refs=None):
    if response.get("tool_calls"):
        raise ValueError("review_tools_forbidden")
    text = visible_text(response.get("content"))
    fence = re.fullmatch(r"```(?:json)?\s*\n(.*?)\n```", text, re.S)
    candidate = fence[1] if fence else text
    try:
        report = json.loads(candidate)
    except json.JSONDecodeError:
        # Some providers wrap valid structured output in an introductory sentence.
        # Accept exactly one complete report; never repair truncated or ambiguous JSON.
        decoder = json.JSONDecoder()
        reports = []
        position = 0
        while position < len(candidate):
            start = candidate.find("{", position)
            if start < 0:
                break
            try:
                value, end = decoder.raw_decode(candidate, start)
            except json.JSONDecodeError:
                position = start + 1
                continue
            if isinstance(value, dict) and {"answers_question", "reason", "blocks"} <= value.keys():
                reports.append(value)
            position = end
        if len(reports) != 1:
            raise ValueError("review_json_invalid") from None
        report = reports[0]
    if (
        not isinstance(report, dict)
        or type(report.get("answers_question")) is not bool
        or not isinstance(report.get("reason"), str)
        or not report["reason"].strip()
        or len(report["reason"]) > 1000
        or not isinstance(report.get("blocks"), list)
        or len(report["blocks"]) != len(blocks)
    ):
        raise ValueError("incomplete_review")
    expected = {block["id"]: block for block in blocks}
    available = {source["id"] for source in sources}
    seen = set()
    validated = []
    for item in report["blocks"]:
        if (
            not isinstance(item, dict)
            or not isinstance(item.get("id"), str)
            or item["id"] not in expected
            or item["id"] in seen
            or item.get("status") not in {"supported", "unsupported", "contradicted", "not_factual"}
            or not isinstance(item.get("source_ids"), list)
            or any(not isinstance(key, str) or key not in available for key in item["source_ids"])
            or not isinstance(item.get("reason"), str)
            or not item["reason"].strip()
            or len(item["reason"]) > 1000
        ):
            raise ValueError("invalid_block_review")
        cited = set(citation_ids(expected[item["id"]]["text"]))
        covered = set(item["source_ids"])
        missing = cited - covered
        if document_refs:
            covered_docs = {document_refs[key] for key in covered}
            missing = {key for key in cited if document_refs[key] not in covered_docs}
        if item["status"] == "supported" and (not covered or missing):
            raise ValueError("unsupported_review_citation")
        if item["status"] == "not_factual" and (cited or item["source_ids"]):
            raise ValueError("factual_block_skipped")
        seen.add(item["id"])
        validated.append(
            {
                "block_id": item["id"],
                "text": expected[item["id"]]["text"],
                "status": item["status"],
                "source_ids": item["source_ids"],
                "reason": visible_text(item["reason"]),
            }
        )
    accepted = report["answers_question"] and all(
        item["status"] in {"supported", "not_factual"} for item in validated
    )
    return {
        "status": "accepted" if accepted else "rejected",
        "answers_question": report["answers_question"],
        "reason": visible_text(report["reason"]),
        "blocks": validated,
    }
