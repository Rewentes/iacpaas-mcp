"""Bounded citation grammar shared by prose parsing and evidence verification."""

import re

REFERENCE = re.compile(r"\[(e\d[^\]\n]{0,255})\]")


def citation_ids(text, limit=64):
    text = re.sub(r"```[\s\S]*?```|`[^`\n]*`", "", text)
    ids = []
    seen = set()
    for bracket in REFERENCE.finditer(text):
        for item in re.split(r"\s*[,;]\s*", bracket[1]):
            match = re.fullmatch(r"e(\d+)(?:\s*[-–—]\s*e?(\d+))?", item.strip())
            if not match:
                raise ValueError("invalid_source_reference")
            first, last = int(match[1]), int(match[2] or match[1])
            if first < 1 or last < first or last - first + 1 > limit:
                raise ValueError("invalid_source_range")
            for index in range(first, last + 1):
                source_id = f"e{index}"
                if source_id not in seen:
                    if len(ids) >= limit:
                        raise ValueError("source_reference_limit")
                    seen.add(source_id)
                    ids.append(source_id)
    return ids


def strip_citations(text):
    return REFERENCE.sub("", text)
