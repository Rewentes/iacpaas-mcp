"""Conversation names derived locally from the first question."""

import re

DEFAULT_TITLES = {"новый чат", "новый диалог", "new chat", "new conversation"}


def is_default_title(title):
    return " ".join(title.split()).casefold() in DEFAULT_TITLES


def topic_title(content, limit=72):
    text = re.sub(r"^[#>*`_\s]+", "", " ".join(content.split()))
    if len(text) <= limit:
        return text or "Диалог"
    short = text[: limit - 1]
    boundary = short.rfind(" ")
    if boundary >= limit // 2:
        short = short[:boundary]
    return short.rstrip(" .,:;") + "…"
