"""Controlled positive review for tests that exercise other agent boundaries."""

import json
import re


async def accepted_review(messages, tools):
    data = json.loads(messages[-1]["content"])
    ids = [source["id"] for source in data["sources"]]
    return {
        "content": json.dumps(
            {
                "answers_question": True,
                "reason": "Положительный сценарий проверки.",
                "blocks": [
                    {
                        "id": block["id"],
                        "status": "supported",
                        "source_ids": re.findall(r"\[(e\d+)\]", block["text"]) or ids,
                        "reason": "Данные контролируемого сценария.",
                    }
                    for block in data["blocks"]
                ],
            }
        )
    }
