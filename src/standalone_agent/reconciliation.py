"""Feedback for bounded author/reviewer rounds; history is context, not evidence."""

import json

from .answers import visible_text
from .prompts import ANSWER_REVISION_NOTE, system_prompt
from .verification import review_messages


def revision_messages(
    question,
    candidate,
    rendered,
    registry,
    provenance,
    history,
    summary,
    remaining_calls,
    final_round=False,
    source_limit=64,
    include_document_bodies=True,
    working_summary=None,
):
    # Keep cited evidence first, then the other normative facts already read.
    # IDs always come from the server-owned registry, never from the draft.
    selected = list(
        dict.fromkeys(
            [
                *(s["id"] for s in (rendered or {}).get("sources", []) if s["id"] in registry),
                *(
                    key
                    for key, s in reversed(registry.items())
                    if s.get("value_kind") == "normative_limit"
                ),
                *reversed(registry),
            ]
        )
    )[:source_limit]
    sources = [registry[key] for key in selected]
    _, request = review_messages(question, candidate, sources, provenance, history, summary)
    packet = json.loads(request[-1]["content"])
    if rendered is not None:
        verification = rendered["verification"]
        packet["previous_answer"] = visible_text(candidate)
        packet["revision_feedback"] = {
            key: verification[key]
            for key in ("status", "reason", "review", "validation", "numeric_check")
            if key in verification
        }
        # Preserve the existing machine-readable numeric feedback contract.
        validation = verification.get("validation", {})
        if "numeric_check" in validation:
            packet["numeric_check"] = validation["numeric_check"]
    packet["final_revision"] = final_round
    packet["omitted_evidence_count"] = len(registry) - len(selected)
    if working_summary:
        packet["working_summary"] = working_summary
    if not include_document_bodies:
        for document in packet["documents"]:
            document["result"] = {
                key: value for key, value in document["result"].items() if key != "data"
            }
            document["data_omitted"] = True
    packet["working_context_note"] = (
        "Это рабочая выборка уже прочитанных источников. Полные результаты сохранены. "
        "data_omitted и omitted_evidence_count означают сокращение рабочего контекста, "
        "а не отсутствие данных в базе. Сохраняй смысл полей, единицы и ограничения. "
        "История нужна для понимания вопроса и не подтверждает факты."
        " working_summary — сводка действий и прочитанных данных для продолжения работы, "
        "а не самостоятельный источник. Подтверждай утверждения текущими sources/documents."
    )
    return [
        {
            "role": "system",
            "content": system_prompt(remaining_calls)
            + ("\n" + ANSWER_REVISION_NOTE if rendered is not None else ""),
        },
        {"role": "user", "content": json.dumps(packet, ensure_ascii=False)},
    ]


def round_report(candidate, rendered, revision):
    verification = rendered["verification"]
    return {
        "revision": revision,
        "candidate_text": visible_text(candidate),
        "status": verification["status"],
        "reason": verification["reason"],
        "source_ids": [source["id"] for source in rendered["sources"]],
        **{key: verification[key] for key in ("review", "validation") if key in verification},
    }


class ReconciliationState:
    """Turn-local revision budget and the best independently checked partial answer."""

    def __init__(self, limit):
        self.limit = limit
        self.revisions = 0
        self.rounds = []
        self.previous_review = None
        self.best_partial = None
        self.stopped_reason = None
        self.tools_disabled = False

    @staticmethod
    def needs_revision(rendered):
        return rendered["verification"]["status"] in {
            "rejected",
            "partial",
            "error",
            "review_unavailable",
            "insufficient_data",
        }

    @staticmethod
    def supported_count(rendered):
        return sum(
            b["status"] == "supported"
            for b in (rendered or {}).get("verification", {}).get("review", {}).get("blocks", [])
        )

    def record(self, candidate, rendered):
        self.rounds.append(round_report(candidate, rendered, self.revisions))
        if rendered["verification"]["status"] == "partial" and (
            self.best_partial is None
            or self.supported_count(rendered) > self.supported_count(self.best_partial)
        ):
            self.best_partial = rendered

    def finish(self, rendered):
        if self.needs_revision(rendered) and self.best_partial is not None:
            rendered = self.best_partial
        rendered["verification"]["reconciliation"] = {
            "revisions": self.revisions,
            "limit": self.limit,
            "resolved": rendered["verification"]["status"]
            in {"reviewed", "verified", "not_applicable"},
            "rounds": self.rounds,
            "stopped_reason": self.stopped_reason,
        }
        return rendered
