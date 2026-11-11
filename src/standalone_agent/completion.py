"""Decode the shared Chat Completions envelope before executing or publishing it."""

from .errors import ConversationError


def completion_message(data):
    try:
        choice = data["choices"][0]
        message = choice["message"]
        if not isinstance(message, dict) or message.get("role") != "assistant":
            raise ValueError
        finish = choice.get("finish_reason")
        # A valid-looking prefix is still incomplete, including tool arguments.
        if finish == "length":
            raise ConversationError("Достигнут лимит вывода модели.", "model_output_limit")
        if finish == "content_filter" or message.get("refusal"):
            raise ConversationError("Провайдер отклонил ответ.", "model_refusal")
        content = message.get("content")
        if isinstance(content, list):
            # Accept text parts only. Never expose reasoning or unknown parts.
            if any(
                not isinstance(part, dict)
                or part.get("type") != "text"
                or not isinstance(part.get("text"), str)
                for part in content
            ):
                raise ValueError
            content = "".join(part["text"] for part in content)
        if content is not None and not isinstance(content, str):
            raise ValueError
        calls = message.get("tool_calls")
        if not calls and not (content and content.strip()):
            raise ConversationError("Провайдер вернул пустой ответ.", "empty_model_output")
        result = {"role": "assistant", "content": content}
        if calls:
            result["tool_calls"] = calls
        return result
    except ConversationError:
        raise
    except (ValueError, KeyError, TypeError, IndexError):
        raise ConversationError(
            "Некорректная структура ответа провайдера.",
            "model_output_error",
            diagnostic="provider_envelope",
        ) from None
