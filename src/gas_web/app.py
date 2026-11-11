"""Same-origin HTTP interface for the persistent conversation service."""

import asyncio
import inspect
import json
import logging
import os
import re
import secrets
import sqlite3
from contextlib import asynccontextmanager, suppress
from ipaddress import ip_address
from pathlib import Path

from starlette.applications import Starlette
from starlette.datastructures import MutableHeaders
from starlette.exceptions import HTTPException
from starlette.middleware.trustedhost import TrustedHostMiddleware
from starlette.responses import FileResponse, JSONResponse, StreamingResponse
from starlette.routing import Route

from .model_settings import ModelSettings, SettingsError
from .platform_settings import PlatformSettings
from .storage import Conflict, Storage

AGENT_FAILURES = {
    "tool_result_limit": "Результат чтения слишком большой. Уточните газ, сорт или характеристику.",
    "context_limit": "Достигнут лимит рабочего контекста. Сузьте запрос или начните новый диалог.",
    "tool_limit": "Достигнут лимит обращений к ресурсам. Уточните газ и нужную характеристику.",
    "model_output_error": "Модель вернула ответ в неподдерживаемом формате. Проверенный ответ не сформирован.",
    "model_output_limit": "Модель исчерпала лимит вывода до завершения ответа. Увеличьте лимит вывода в настройках запуска или сузьте запрос.",
    "empty_model_output": "Провайдер вернул пустой ответ модели. Проверенный ответ не сформирован.",
    "model_refusal": "Провайдер отклонил формирование ответа. Проверенный ответ не сформирован.",
    "provider_unavailable": "Не удалось получить ответ модели. Проверьте доступность провайдера.",
    "agent_error": "Не удалось завершить обработку запроса к модели или MCP.",
}


class SecurityHeaders:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        async def secured(message):
            if message["type"] == "http.response.start":
                headers = MutableHeaders(scope=message)
                headers["X-Content-Type-Options"] = "nosniff"
                headers["Content-Security-Policy"] = (
                    "default-src 'self'; img-src 'self' data:; object-src 'none'; base-uri 'none'; frame-ancestors 'none'; form-action 'self'"
                )
                if scope["path"].startswith("/api/"):
                    headers["Cache-Control"] = "no-store"
            await send(message)

        await self.app(scope, receive, secured)


class APIError(HTTPException):
    def __init__(self, status_code, payload):
        super().__init__(status_code)
        self.payload = payload


def default_db_path():
    return (
        Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local" / "share"))
        / "iacpaas-web"
        / "chat.sqlite3"
    )


async def json_body(request):
    if request.headers.get("content-type", "").split(";")[0].strip().lower() != "application/json":
        raise APIError(
            415, {"code": "invalid_request", "message": "Use application/json.", "retryable": False}
        )
    data = bytearray()
    async for chunk in request.stream():
        if len(data) + len(chunk) > 131072:
            raise APIError(
                413,
                {
                    "code": "request_too_large",
                    "message": "JSON body exceeds 128 KiB.",
                    "retryable": False,
                },
            )
        data.extend(chunk)
    try:
        body = json.loads(data)
        json.dumps(body, ensure_ascii=False, allow_nan=False).encode("utf-8")
    except (ValueError, RecursionError):
        raise APIError(
            400, {"code": "invalid_request", "message": "Invalid JSON body.", "retryable": False}
        ) from None
    if not isinstance(body, dict):
        raise APIError(
            400,
            {
                "code": "invalid_request",
                "message": "JSON body must be an object.",
                "retryable": False,
            },
        )
    return body


def trusted_host_names(bind_host=None, configured=None):
    allowed_hosts = ["localhost", "127.0.0.1", "[::1]"]
    if bind_host and bind_host != "localhost":
        address = ip_address(bind_host)
        if not address.is_unspecified:
            allowed_hosts.append(f"[{address}]" if address.version == 6 else str(address))
    if configured is None:
        configured = os.environ.get("IACPAAS_TRUSTED_HOSTS", "").split(",")
    if isinstance(configured, str):
        configured = configured.split(",")
    if not isinstance(configured, (list, tuple)) or len(configured) > 32:
        raise ValueError("Укажите не более 32 доверенных имён или IP-адресов.")
    for value in configured:
        if not isinstance(value, str):
            raise ValueError("Некорректный доверенный Host.")
        value = value.strip().lower()
        if not value:
            continue
        try:
            address = ip_address(value.removeprefix("[").removesuffix("]"))
        except ValueError:
            if len(value) > 253 or not re.fullmatch(
                r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?(?:\.[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?)*",
                value,
            ):
                raise ValueError(
                    "Доверенный Host должен быть точным именем или IP без порта и wildcard."
                ) from None
        else:
            if address.is_unspecified:
                raise ValueError("Адрес привязки 0.0.0.0 или :: не является доверенным Host.")
            value = f"[{address}]" if address.version == 6 else str(address)
        allowed_hosts.append(value)
    return list(dict.fromkeys(allowed_hosts))


def create_app(*, db_path=None, agent=None, bind_host=None, trusted_hosts=None):
    allowed_hosts = trusted_host_names(bind_host, trusted_hosts)
    store = Storage(db_path if db_path is not None else default_db_path())
    settings = ModelSettings(store)
    platform = PlatformSettings(store)

    @asynccontextmanager
    async def lifespan(app):
        try:
            yield
        finally:
            await settings.close()

    async def me(request):
        owner = store.owner(request.cookies.get("gas_owner"))
        token = None
        if owner is None:
            owner, token = store.new_owner()
        profile = settings.snapshot(owner["id"])
        response = JSONResponse(
            {
                "user_id": owner["id"],
                "csrf_token": owner["csrf"],
                "model": profile.model,
                "provider": profile.provider,
                "backend": "MCP HTTP (backend not verified)"
                if os.environ.get("MCP_TRANSPORT", "stdio") == "http"
                else "MCP stdio (IACPaaS; проверка при чтении)"
                if os.environ.get("IACPAAS_BACKEND", "mock") == "iacpaas"
                else "MCP stdio (local mock)",
            }
        )
        response.headers["Cache-Control"] = "no-store"
        if token:
            response.set_cookie(
                "gas_owner",
                token,
                httponly=True,
                samesite="strict",
                secure=request.url.scheme == "https",
                max_age=31536000,
            )
        return response

    async def health(request):
        with store.connect() as database:
            database.execute("SELECT 1 FROM owners LIMIT 1").fetchone()
        return JSONResponse({"status": "ok"}, headers={"Cache-Control": "no-store"})

    def authenticate(request):
        owner = store.owner(request.cookies.get("gas_owner"))
        if owner is None:
            raise APIError(
                401, {"code": "unauthorized", "message": "Call /api/me first.", "retryable": False}
            )
        if request.method == "POST" and not secrets.compare_digest(
            request.headers.get("X-CSRF-Token", "").encode(), owner["csrf"].encode()
        ):
            raise APIError(
                403, {"code": "csrf_error", "message": "Invalid CSRF token.", "retryable": False}
            )
        origin = request.headers.get("origin")
        if (
            request.method == "POST"
            and origin is not None
            and origin != str(request.base_url).rstrip("/")
        ):
            raise APIError(
                403,
                {
                    "code": "csrf_error",
                    "message": "Cross-origin writes are not allowed.",
                    "retryable": False,
                },
            )
        return owner

    async def model_settings(request):
        owner = authenticate(request)
        if request.method == "POST":
            try:
                settings.save(owner["id"], await json_body(request))
            except ValueError:
                raise APIError(
                    400,
                    {
                        "code": "invalid_request",
                        "message": "Некорректные настройки модели.",
                        "retryable": False,
                    },
                ) from None
        return JSONResponse(await settings.public(owner["id"]))

    async def platform_settings(request):
        owner = authenticate(request)
        if request.method == "POST":
            try:
                platform.save(owner["id"], await json_body(request))
            except ValueError as exc:
                raise APIError(400, {"code": "invalid_request", "message": str(exc), "retryable": False}) from None
        return JSONResponse(platform.public(owner["id"]))

    async def test_platform(request):
        owner = authenticate(request)
        return JSONResponse(await platform.test(owner["id"]))

    async def models(request):
        owner = authenticate(request)
        return JSONResponse(
            await settings.models(owner["id"], request.query_params.get("provider"))
        )

    async def test_settings(request):
        owner = authenticate(request)
        if await json_body(request):
            raise APIError(
                400,
                {"code": "invalid_request", "message": "Use an empty object.", "retryable": False},
            )
        return JSONResponse(await settings.test(owner["id"]))

    async def codex_status(request):
        owner = authenticate(request)
        return JSONResponse(await settings.codex_status(owner["id"]))

    async def codex_action(request):
        owner = authenticate(request)
        if await json_body(request):
            raise APIError(
                400,
                {"code": "invalid_request", "message": "Use an empty object.", "retryable": False},
            )
        action = (
            settings.codex_login
            if request.url.path.endswith("/login/start")
            else settings.codex_logout
        )
        return JSONResponse(await action(owner["id"]))

    async def conversations(request):
        owner = authenticate(request)
        if request.method == "GET":
            return JSONResponse({"items": store.conversations(owner["id"])})
        body = await json_body(request)
        title = body.get("title") if isinstance(body, dict) else None
        if not isinstance(title, str) or not title.strip() or len(title) > 200:
            raise APIError(
                400,
                {
                    "code": "invalid_request",
                    "message": "title must be 1–200 characters.",
                    "retryable": False,
                },
            )
        return JSONResponse(store.create_conversation(owner["id"], title.strip()), status_code=201)

    async def manage_conversation(request):
        owner = authenticate(request)
        body = await json_body(request)
        action = body.get("action")
        if action not in ("archive", "restore", "delete") or set(body) != {"action"}:
            raise APIError(400, {"code": "invalid_request", "message": "Неизвестное действие.", "retryable": False})
        try:
            found = store.manage_conversation(owner["id"], request.path_params["conversation_id"], action)
        except Conflict as exc:
            raise APIError(409, {"code": "conflict", "message": str(exc), "retryable": False}) from None
        if not found:
            raise APIError(404, {"code": "not_found", "message": "Диалог не найден.", "retryable": False})
        return JSONResponse({"ok": True, "action": action})

    async def revisions(request):
        owner = authenticate(request)
        body = await json_body(request)
        action = body.get("action")
        message_id, request_id = body.get("message_id"), body.get("request_id")
        content = body.get("content")
        if (
            set(body) - {"action", "message_id", "request_id", "content"}
            or action not in ("edit", "regenerate")
            or any(
                not isinstance(value, str) or not value or len(value) > 200
                for value in (message_id, request_id)
            )
            or action == "edit"
            and (not isinstance(content, str) or not content.strip() or len(content) > 20000)
            or action == "regenerate"
            and content is not None
        ):
            raise APIError(
                400,
                {
                    "code": "invalid_request",
                    "message": "Некорректный запрос новой версии.",
                    "retryable": False,
                },
            )
        try:
            result = store.revise(
                owner["id"],
                request.path_params["conversation_id"],
                request_id,
                message_id,
                action,
                content,
            )
        except Conflict as exc:
            raise APIError(
                409, {"code": "conflict", "message": str(exc), "retryable": False}
            ) from None
        except ValueError as exc:
            raise APIError(
                400, {"code": "invalid_request", "message": str(exc), "retryable": False}
            ) from None
        if result is None:
            raise APIError(
                404, {"code": "not_found", "message": "Сообщение не найдено.", "retryable": False}
            )
        return JSONResponse(result)

    async def messages(request):
        owner = authenticate(request)
        conversation = store.conversation(owner["id"], request.path_params["conversation_id"])
        if conversation is None:
            raise APIError(
                404, {"code": "not_found", "message": "Conversation not found.", "retryable": False}
            )
        if request.method == "POST":
            body = await json_body(request)
            content = body.get("content") if isinstance(body, dict) else None
            request_id = body.get("request_id") if isinstance(body, dict) else None
            if (
                not isinstance(content, str)
                or not content.strip()
                or len(content) > 20000
                or not isinstance(request_id, str)
                or not request_id
                or len(request_id) > 200
            ):
                raise APIError(
                    400,
                    {
                        "code": "invalid_request",
                        "message": "content and request_id are required.",
                        "retryable": False,
                    },
                )
            try:
                user_message, replay, status = store.start_turn(
                    conversation["id"], request_id, content
                )
            except Conflict as exc:
                raise APIError(
                    409, {"code": "conflict", "message": str(exc), "retryable": False}
                ) from None
            if replay is not None:
                assert status is not None
                return JSONResponse(replay, status_code=status)
            assert user_message is not None
            previous = [
                message
                for message in store.history(conversation["id"])
                if message["id"] != user_message["id"]
            ]

            async def execute(on_event=None):
                try:
                    service = app.state.agent
                    if service is None:
                        from standalone_agent.conversation import ConversationAgent

                        profile = settings.snapshot(owner["id"])
                        service = ConversationAgent(
                            provider=settings.provider(profile),
                            reviewer=settings.provider(profile, review=True),
                            session_factory=platform.session_factory(owner["id"]),
                        )
                    kwargs = {"summary": store.summary(conversation["id"])}
                    if (
                        on_event is not None
                        and "on_event" in inspect.signature(service.respond).parameters
                    ):
                        kwargs["on_event"] = on_event
                    result = await service.respond(content, previous, **kwargs)
                    return JSONResponse(
                        store.finish_turn(conversation["id"], request_id, user_message, result)
                    )
                except asyncio.CancelledError:
                    response = {
                        "error": {
                            "code": "interrupted",
                            "message": "Соединение прервано. Сообщение сохранено; автоматического повтора нет.",
                            "retryable": False,
                        }
                    }
                    store.fail_turn(conversation["id"], request_id, user_message, response, 502)
                    raise
                except Exception as exc:
                    configuration = getattr(exc, "code", None) == "configuration_error"
                    code = getattr(exc, "code", "agent_error")
                    if code not in AGENT_FAILURES:
                        code = "agent_error"
                    logging.getLogger(__name__).warning(
                        "Conversation failed: %s (%s)",
                        "configuration_error" if configuration else code,
                        getattr(exc, "diagnostic", None) or "unspecified",
                    )
                    response = {
                        "error": {
                            "code": "configuration_error" if configuration else code,
                            "message": "Настройте ключ выбранного провайдера (OPENROUTER_API_KEY / OPENAI_API_KEY) или вход Codex и MCP backend."
                            if configuration
                            else AGENT_FAILURES[code]
                            + " Сообщение сохранено. Повторный запрос автоматически не отправлялся.",
                            "retryable": False,
                        }
                    }
                    status = 503 if configuration else 502
                    store.fail_turn(conversation["id"], request_id, user_message, response, status)
                    return JSONResponse(response, status_code=status)

            if "text/event-stream" not in request.headers.get("accept", ""):
                return await execute()

            async def stream():
                queue = asyncio.Queue(maxsize=128)

                async def publish(event):
                    await queue.put(("progress", event))

                async def run():
                    try:
                        response = await execute(publish)
                    except Exception:
                        logging.getLogger(__name__).warning("Streamed turn could not be persisted")
                        response = JSONResponse(
                            {
                                "error": {
                                    "code": "storage_error",
                                    "message": "Не удалось сохранить результат. Проверьте историю перед повтором.",
                                    "retryable": False,
                                }
                            },
                            status_code=503,
                        )
                    await queue.put(
                        (
                            "done",
                            {"status": response.status_code, "result": json.loads(response.body)},
                        )
                    )

                task = asyncio.create_task(run())
                try:
                    while True:
                        try:
                            kind, data = await asyncio.wait_for(queue.get(), timeout=15)
                        except TimeoutError:
                            yield ": keepalive\n\n"
                            continue
                        yield (
                            "event: "
                            + kind
                            + "\ndata: "
                            + json.dumps(data, ensure_ascii=False)
                            + "\n\n"
                        )
                        if kind == "done":
                            break
                finally:
                    if not task.done():
                        task.cancel()
                    with suppress(asyncio.CancelledError):
                        await task

            return StreamingResponse(
                stream(),
                media_type="text/event-stream",
                headers={
                    "Cache-Control": "no-store",
                    "X-Accel-Buffering": "no",
                },
            )
        try:
            limit = int(request.query_params.get("limit", "50"))
            if not 1 <= limit <= 200:
                raise ValueError
            page = store.page(conversation["id"], limit, request.query_params.get("before"))
        except ValueError:
            raise APIError(
                400,
                {
                    "code": "invalid_request",
                    "message": "Invalid limit or history cursor.",
                    "retryable": False,
                },
            ) from None
        return JSONResponse(
            {"conversation": conversation, **page, "context": store.context(conversation["id"])}
        )

    async def frontend(request):
        filename = request.path_params.get("filename", "index.html")
        if filename not in (
            "index.html",
            "app.js",
            "markdown.js",
            "starter_questions.js",
            "api.js",
            "settings.js",
            "platform_settings.js",
            "styles.css",
            "theme.js",
            "ring.js",
            "ring.css",
            "ring.js.LEGAL.txt",
            "ring.css.LEGAL.txt",
        ):
            if not re.fullmatch(r"inter-[a-z0-9-]+-[A-Z0-9]{8}\.woff2?", filename):
                raise HTTPException(404)
        path = Path(__file__).parent / "static" / filename
        if not path.is_file():
            raise HTTPException(404)
        return FileResponse(path)

    async def http_error(request, exc):
        detail = (
            exc.payload
            if isinstance(exc, APIError)
            else {
                "code": "not_found" if exc.status_code == 404 else "http_error",
                "message": "Not found." if exc.status_code == 404 else "Request rejected.",
                "retryable": False,
            }
        )
        return JSONResponse({"error": detail}, status_code=exc.status_code)

    async def settings_error(request, exc):
        return JSONResponse(
            {
                "error": {
                    "code": exc.code,
                    "message": str(exc),
                    "retryable": False,
                }
            },
            status_code=exc.status,
        )

    async def server_error(request, exc):
        storage_failure = isinstance(exc, sqlite3.Error)
        return JSONResponse(
            {
                "error": {
                    "code": "storage_error" if storage_failure else "internal_error",
                    "message": "Storage is unavailable; do not resubmit with a new request_id."
                    if storage_failure
                    else "Internal server error; do not resubmit with a new request_id.",
                    "retryable": False,
                }
            },
            status_code=503 if storage_failure else 500,
            headers={"Cache-Control": "no-store"},
        )

    app = Starlette(
        lifespan=lifespan,
        routes=[
            Route("/healthz", health),
            Route("/api/me", me),
            Route("/api/settings", model_settings, methods=["GET", "POST"]),
            Route("/api/models", models),
            Route("/api/platform/settings", platform_settings, methods=["GET", "POST"]),
            Route("/api/platform/settings/test", test_platform, methods=["POST"]),
            Route("/api/settings/test", test_settings, methods=["POST"]),
            Route("/api/codex/status", codex_status),
            Route("/api/codex/login/start", codex_action, methods=["POST"]),
            Route("/api/codex/logout", codex_action, methods=["POST"]),
            Route("/api/conversations", conversations, methods=["GET", "POST"]),
            Route("/api/conversations/{conversation_id}/manage", manage_conversation, methods=["POST"]),
            Route("/api/conversations/{conversation_id}/revisions", revisions, methods=["POST"]),
            Route(
                "/api/conversations/{conversation_id}/messages", messages, methods=["GET", "POST"]
            ),
            Route("/", frontend),
            Route("/static/{filename}", frontend),
            Route("/{filename}", frontend),
        ],
        exception_handlers={
            SettingsError: settings_error,
            HTTPException: http_error,
            sqlite3.Error: server_error,
            Exception: server_error,
        },
    )
    app.add_middleware(
        TrustedHostMiddleware,
        allowed_hosts=allowed_hosts,
        www_redirect=False,
    )
    app.add_middleware(SecurityHeaders)
    app.state.storage = store
    app.state.settings = settings
    app.state.platform = platform
    app.state.agent = agent
    return app
