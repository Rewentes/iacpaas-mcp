"""Safe provider failures: status categories only, never upstream bodies or credentials."""

import httpx

from .errors import ConversationError

PROVIDER_FAILURES = {
    "provider_endpoint_required": "Укажите адрес OpenAI-совместимого API в настройках.",
    "provider_model_required": "Выберите модель или введите её ID в настройках.",
    "provider_mode_unsupported": "Выбрана пакетная модель (:batch). Для этого чата выберите обычную версию без :batch в настройках.",
    "provider_auth_error": "Провайдер отклонил авторизацию (HTTP 401). Проверьте ключ и права доступа в настройках.",
    "provider_access_denied": "Провайдер запретил запрос (HTTP 403). Возможны ограничения доступа или правила обработки запроса; этот код сам по себе не означает неверный ключ.",
    "provider_credit_limit": "Провайдер сообщил о недостаточном балансе или лимите расходов ключа.",
    "provider_rate_limit": "Провайдер ограничил частоту запросов. Повторите запрос позже.",
    "provider_model_unavailable": "Выбранная модель недоступна у провайдера. Проверьте её идентификатор и доступ в настройках.",
    "provider_request_error": "Провайдер отклонил параметры запроса. Проверьте выбранную модель и поддерживаемые ею возможности.",
    "provider_timeout": "Истекло время ожидания ответа провайдера модели.",
    "provider_response_error": "Провайдер вернул некорректный ответ. Проверенный ответ не сформирован.",
    "provider_unavailable": "Провайдер модели временно недоступен или соединение с ним прервалось.",
}


def is_batch_model(provider, model):
    return provider == "openrouter" and "batch" in model.split(":")[1:]


def require_interactive_model(provider, model):
    if is_batch_model(provider, model):
        raise ConversationError(
            PROVIDER_FAILURES["provider_mode_unsupported"],
            "provider_mode_unsupported",
            diagnostic="batch_in_interactive_chat",
        )


def http_failure(status):
    code = {
        400: "provider_request_error",
        401: "provider_auth_error",
        402: "provider_credit_limit",
        403: "provider_access_denied",
        404: "provider_model_unavailable",
        422: "provider_request_error",
        429: "provider_rate_limit",
    }.get(status, "provider_unavailable")
    return ConversationError(PROVIDER_FAILURES[code], code, diagnostic=f"provider_http_{status}")


def transport_failure(exc):
    if isinstance(exc, httpx.HTTPStatusError):
        return http_failure(exc.response.status_code)
    code = "provider_timeout" if isinstance(exc, httpx.TimeoutException) else "provider_unavailable"
    return ConversationError(PROVIDER_FAILURES[code], code, diagnostic=code)


def check_error_envelope(data):
    """Some providers report errors inside a successful HTTP response."""
    if not isinstance(data, dict) or data.get("error") is None:
        return
    error = data["error"]
    status = error.get("code") if isinstance(error, dict) else None
    if isinstance(status, str) and len(status) == 3 and status.isascii() and status.isdigit():
        status = int(status)
    if type(status) is int and 400 <= status <= 599:
        raise http_failure(status)
    raise ConversationError(
        PROVIDER_FAILURES["provider_response_error"],
        "provider_response_error",
        diagnostic="provider_error_envelope",
    )
