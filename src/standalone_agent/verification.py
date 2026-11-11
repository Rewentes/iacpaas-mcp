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
    blocks = []
    for block in re.split(r"\n\s*\n|\n(?=\s*[-*]\s)", text):
        block = block.strip()
        if not block:
            continue
        if blocks and block.startswith("|") and blocks[-1].endswith(":"):
            blocks[-1] = blocks[-1] + "\n" + block
        else:
            blocks.append(block)
    return [{"id": f"b{index + 1}", "text": block} for index, block in enumerate(blocks)]


def source_context(source, result):
    data = result.get("data") or {}
    context = {key: data[key] for key in ("gas_name", "standard") if key in data}
    return context


UNIT_PATTERN = re.compile(
    r"(?<![\w])(?:%|ppm|мг\s*/\s*м[³3]|г\s*/\s*м[³3]|кг\s*/\s*м[³3]|"
    r"МПа|кПа|Па|бар|°[CС]|процент(?:ов|а)?|об\.?\s*%)(?!\w)",
    re.I,
)
NUMBER_PATTERN = r"(?<![\w])[+-]?\d+(?:[.,]\d+)?"
CITATION_PATTERN = r"\[e\d+\]"


def _normalized(text):
    return re.sub(r"[\s*`_]", "", text).casefold()


def _is_value_cell(text):
    return bool(re.search(CITATION_PATTERN, text) or re.search(NUMBER_PATTERN, text))


def _table_context(text, citation_start):
    """Return the table cell with the citation, its row label, header and unit cells."""
    row_start = text.rfind("\n", 0, citation_start) + 1
    row_end = text.find("\n", citation_start)
    if row_end < 0:
        row_end = len(text)
    row = text[row_start:row_end]
    if not row.lstrip().startswith("|"):
        return None
    position = citation_start - row_start
    parts = row.split("|")
    cell_index = sum(1 for index, char in enumerate(row) if char == "|" and index < position)
    cell = parts[cell_index] if cell_index < len(parts) else ""
    labels = [part.strip() for part in parts if part.strip()]
    label = labels[0] if labels else ""
    header = ""
    for line in reversed(text[:row_start].splitlines()):
        if not line.lstrip().startswith("|"):
            break
        header_parts = line.split("|")
        if len(header_parts) != len(parts):
            continue
        if all(set(part.strip()) <= set("-: ") for part in header_parts if part.strip()):
            continue
        if re.search(CITATION_PATTERN, line):
            continue
        header = header_parts[cell_index].strip() if cell_index < len(header_parts) else ""
        break
    caption = ""
    lines_before = text[:row_start].splitlines()
    index = len(lines_before) - 1
    while index >= 0 and lines_before[index].lstrip().startswith("|"):
        index -= 1
    while index >= 0 and not lines_before[index].strip():
        index -= 1
    if index >= 0 and lines_before[index].strip().endswith(":"):
        caption = lines_before[index].strip()
    left = []
    for part in reversed(parts[:cell_index]):
        text_part = part.strip()
        if text_part and not _is_value_cell(text_part):
            left.append(text_part)
    units = []
    for index, part in enumerate(parts):
        text_part = part.strip()
        if index != cell_index and text_part and not _is_value_cell(text_part):
            units.append(text_part)
    return {
        "cell": cell,
        "label": label,
        "header": header,
        "caption": caption,
        "left": left,
        "units": units,
    }


def _clause_prefix(text, citation_start):
    """Return the prose governing a citation, up to the previous citation."""
    return re.split(CITATION_PATTERN, text[:citation_start])[-1]


def normative_issues(blocks, sources):
    """Catch local numeric/unit/bound substitutions before the model-based review."""
    issues = []
    bound_pattern = r"не\s+более|не\s+менее|не\s+выше|не\s+ниже|≤|≥|<=|>=|максимум|минимум"
    expected = {
        "≤": {"не более", "не выше", "≤", "<=", "максимум"},
        "≥": {"не менее", "не ниже", "≥", ">=", "минимум"},
    }
    for block in blocks:
        for source in sources:
            if source.get("value_kind") != "normative_limit":
                continue
            citation = "[" + source["id"] + "]"
            if citation not in block["text"]:
                continue
            citation_start = block["text"].index(citation)
            table = _table_context(block["text"], citation_start)
            context = table["cell"] if table else _clause_prefix(block["text"], citation_start)
            for match in re.finditer(NUMBER_PATTERN, context):
                if Decimal(match[0].replace(",", ".")) != Decimal(str(source["value"])):
                    continue
                before, after = context[: match.start()], context[match.end() :]
                bounds = list(re.finditer(bound_pattern, before, re.I))
                if table and not bounds:
                    for part in [*table["left"], table["label"]]:
                        bounds = list(re.finditer(bound_pattern, part, re.I))
                        if bounds:
                            break
                actual = bounds[-1][0].lower() if bounds else ""
                if re.sub(r"\s+", " ", actual) not in expected.get(source.get("operator"), set()):
                    issues.append(
                        {
                            "block_id": block["id"],
                            "source_id": source["id"],
                            "code": "bound_mismatch",
                        }
                    )
                unit = _normalized(source.get("unit") or "")
                following = _normalized(after)
                aliases = (
                    ("%", "процент", "об.%", "объемныхпроцент", "объёмныхпроцент")
                    if unit == "%"
                    else (unit,)
                )
                if table and (not following or following[0] in "[](),.;:"):
                    # A table may carry the unit in a row label, column header or unit cell.
                    unit_ok = False
                    for candidate in [
                        table["label"],
                        table["header"],
                        *table["units"],
                        table["caption"],
                    ]:
                        if not candidate:
                            continue
                        if any(alias in _normalized(candidate) for alias in aliases):
                            unit_ok = True
                            break
                        if UNIT_PATTERN.search(candidate):
                            break
                else:
                    unit_ok = following.startswith(aliases)
                if unit and not unit_ok:
                    issues.append(
                        {
                            "block_id": block["id"],
                            "source_id": source["id"],
                            "code": "unit_mismatch",
                        }
                    )
    return list(
        {(item["block_id"], item["source_id"], item["code"]): item for item in issues}.values()
    )


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
