"""Owner-isolated model profiles; secrets never form part of a public response."""

import hashlib
import json
import os
import re
from dataclasses import dataclass, field
from urllib.parse import urlsplit

import httpx

from standalone_agent.conversation import ConversationError
from standalone_agent.review_options import openrouter_review_options

PROVIDERS = [
    {"id": "openrouter", "name": "OpenRouter", "auth_type": "api_key"},
    {"id": "openai", "name": "OpenAI API", "auth_type": "api_key"},
    {"id": "codex", "name": "Codex / ChatGPT", "auth_type": "chatgpt"},
]
DEFAULT_MODEL = "deepseek/deepseek-v4.1-flash"
KEY_ENV = {"openrouter": "OPENROUTER_API_KEY", "openai": "OPENAI_API_KEY"}
MODEL_ID = re.compile(r"[A-Za-z0-9~][A-Za-z0-9._:/@+~\-]{0,199}\Z")
BASE_URL = {"openrouter": "https://openrouter.ai/api/v1", "openai": "https://api.openai.com/v1"}


class SettingsError(ValueError):
    def __init__(self, message, code="provider_unavailable", status=502):
        super().__init__(message)
        self.code, self.status = code, status


def reject_credential_echo(value, key):
    if key and json.dumps(key, ensure_ascii=False)[1:-1] in json.dumps(value, ensure_ascii=False):
        raise ValueError("Provider returned a credential")


async def api_request(provider, path, *, api_key="", payload=None):
    headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
    async with httpx.AsyncClient(follow_redirects=False, trust_env=False) as client:
        async with client.stream(
            "GET" if payload is None else "POST",
            BASE_URL[provider] + path,
            headers=headers,
            json=payload,
            timeout=60,
        ) as response:
            response.raise_for_status()
            data = bytearray()
            async for chunk in response.aiter_bytes():
                data.extend(chunk)
                if len(data) > 4 * 1024 * 1024:
                    raise ValueError("Provider response exceeds limit")
            return json.loads(data)


@dataclass(frozen=True)
class Profile:
    owner_id: str
    provider: str
    model: str
    api_key: str = field(repr=False)
    connection_status: str = "not_configured"

    def signature(self):
        return hashlib.sha256(json.dumps([self.model, self.api_key]).encode()).hexdigest()


class ModelSettings:
    def __init__(self, store):
        self.store = store
        self.codex = None
        with store.connect() as db:
            db.execute("""CREATE TABLE IF NOT EXISTS model_selection (
                owner_id TEXT PRIMARY KEY REFERENCES owners(id), provider TEXT NOT NULL)""")
            db.execute("""CREATE TABLE IF NOT EXISTS model_profiles (
                owner_id TEXT NOT NULL REFERENCES owners(id), provider TEXT NOT NULL,
                model TEXT NOT NULL, api_key TEXT, connection_status TEXT NOT NULL,
                PRIMARY KEY(owner_id,provider))""")

    def snapshot(self, owner_id, provider=None):
        with self.store.connect() as db:
            if provider is None:
                row = db.execute(
                    "SELECT provider FROM model_selection WHERE owner_id=?", (owner_id,)
                ).fetchone()
                provider = row[0] if row else "openrouter"
            row = db.execute(
                "SELECT model,api_key,connection_status FROM model_profiles WHERE owner_id=? AND provider=?",
                (owner_id, provider),
            ).fetchone()
        key = (
            row["api_key"]
            if row and row["api_key"] is not None
            else os.environ.get(KEY_ENV.get(provider, ""), "").strip()
        )
        profile = Profile(
            owner_id,
            provider,
            row["model"] if row else DEFAULT_MODEL,
            key,
        )
        status = "configured" if key else "not_configured"
        if row and row["connection_status"] in (
            "connected:" + profile.signature(),
            "error:" + profile.signature(),
        ):
            status = row["connection_status"].split(":")[0]
        return Profile(owner_id, provider, profile.model, key, status)

    def save(self, owner_id, body):
        provider, model = body.get("provider"), body.get("model")
        if (
            set(body) - {"provider", "model", "api_key", "clear_api_key"}
            or not isinstance(provider, str)
            or provider not in {p["id"] for p in PROVIDERS}
            or not isinstance(model, str)
            or not MODEL_ID.fullmatch(model)
        ):
            raise ValueError("Укажите провайдера и модель.")
        key = body.get("api_key", "")
        clear = body.get("clear_api_key", False)
        if (
            not isinstance(key, str)
            or len(key) > 8192
            or any(ord(c) < 32 or ord(c) > 126 for c in key)
            or type(clear) is not bool
            or clear
            and key.strip()
            or provider == "codex"
            and (key.strip() or clear)
        ):
            raise ValueError("Некорректный API-ключ.")
        key = key.strip()
        with self.store.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            old = db.execute(
                "SELECT api_key FROM model_profiles WHERE owner_id=? AND provider=?",
                (owner_id, provider),
            ).fetchone()
            stored_key = old[0] if old else None
            if clear:
                stored_key = ""
            elif key:
                stored_key = key
            effective = (
                stored_key
                if stored_key is not None
                else os.environ.get(KEY_ENV.get(provider, ""), "")
            )
            db.execute(
                "INSERT OR REPLACE INTO model_profiles VALUES (?,?,?,?,?)",
                (
                    owner_id,
                    provider,
                    model,
                    stored_key,
                    "configured" if effective else "not_configured",
                ),
            )
            db.execute("INSERT OR REPLACE INTO model_selection VALUES (?,?)", (owner_id, provider))

    async def public(self, owner_id):
        profile = self.snapshot(owner_id)
        unavailable = False
        try:
            codex = await self.codex_status(owner_id)
        except SettingsError as exc:
            unavailable = True
            codex = {"installed": getattr(exc, "installed", False), "connected": False}
        status = profile.connection_status
        if profile.provider == "codex":
            status = (
                "unavailable"
                if unavailable or not codex["installed"]
                else "connected"
                if codex["connected"]
                else "not_configured"
            )
        return {
            "provider": profile.provider,
            "model": profile.model,
            "api_key_set": bool(profile.api_key),
            "connection_status": status,
            "providers": PROVIDERS,
            "codex": {key: codex[key] for key in ("installed", "connected")},
        }

    async def models(self, owner_id, provider):
        if provider not in {p["id"] for p in PROVIDERS}:
            raise SettingsError("Неизвестный провайдер.", "invalid_request", 400)
        profile = self.snapshot(owner_id, provider)
        if provider == "openai" and not profile.api_key:
            raise SettingsError("Сохраните API-ключ OpenAI.", "configuration_error", 503)
        try:
            if provider == "codex":
                rows = await self.codex_pool().models(owner_id)
            else:
                data = await api_request(
                    provider, "/models", api_key=profile.api_key if provider == "openai" else ""
                )
                rows = data["data"]
            reject_credential_echo(rows, profile.api_key)
            if not isinstance(rows, list) or len(rows) > 5000:
                raise ValueError("Invalid catalogue")
            items = {}
            for row in rows:
                model = row["id"]
                name = row.get("name", model)
                if (
                    not isinstance(model, str)
                    or not MODEL_ID.fullmatch(model)
                    or not isinstance(name, str)
                    or not 1 <= len(name) <= 300
                ):
                    raise ValueError("Invalid catalogue entry")
                items[model] = {"id": model, "name": name}
            return {"items": list(items.values()), "source": "catalog", "verified_access": False}
        except (httpx.HTTPError, ValueError, KeyError, TypeError):
            raise SettingsError(
                "Каталог недоступен. Проверьте ключ и сеть; идентификатор модели можно ввести вручную.",
                "catalog_unavailable",
            ) from None

    def provider(self, profile, *, output_tokens=None, review=False):
        if output_tokens is None:
            output_tokens = int(os.environ.get("AGENT_OUTPUT_RESERVE", "4096"))
        if profile.provider == "codex":

            async def codex_complete(messages, tools):
                return await self.codex_pool().complete(
                    profile.owner_id, profile.model, messages, tools
                )

            return codex_complete
        if not profile.api_key:
            raise ConversationError(
                "Сохраните API-ключ выбранного провайдера.", "configuration_error"
            )

        async def complete(messages, tools):
            payload = {"model": profile.model, "messages": messages}
            if review and profile.provider == "openrouter":
                payload.update(await openrouter_review_options(profile.model))
            payload["max_completion_tokens" if profile.provider == "openai" else "max_tokens"] = (
                output_tokens
            )
            if tools:
                payload.update(tools=tools, tool_choice="auto")
            try:
                data = await api_request(
                    profile.provider, "/chat/completions", api_key=profile.api_key, payload=payload
                )
                message = data["choices"][0]["message"]
                reject_credential_echo(message, profile.api_key)
                if (
                    not isinstance(message, dict)
                    or message.get("role") != "assistant"
                    or not isinstance(message.get("content"), (str, type(None)))
                    or message.get("content") is None
                    and not message.get("tool_calls")
                ):
                    raise ValueError("Invalid assistant response")
                return message
            except (httpx.HTTPError, ValueError, KeyError, TypeError, IndexError):
                raise ConversationError("Запрос к выбранному провайдеру не выполнен.") from None

        return complete

    def record_test(self, profile, status):
        with self.store.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            if self.snapshot(profile.owner_id, profile.provider) != profile:
                return
            db.execute(
                """INSERT INTO model_profiles (owner_id,provider,model,api_key,connection_status)
                VALUES (?,?,?,?,?) ON CONFLICT(owner_id,provider) DO UPDATE
                SET connection_status=excluded.connection_status""",
                (
                    profile.owner_id,
                    profile.provider,
                    profile.model,
                    None,
                    status + ":" + profile.signature(),
                ),
            )

    async def test(self, owner_id):
        profile = self.snapshot(owner_id)
        try:
            complete = self.provider(profile, output_tokens=64)
            await complete([{"role": "user", "content": "Reply OK."}], [])
        except ConversationError as exc:
            if exc.code == "configuration_error":
                raise SettingsError(
                    "Сохраните API-ключ выбранного провайдера.", "configuration_error", 503
                ) from None
            self.record_test(profile, "error")
            raise SettingsError(
                "Проверка не пройдена. Проверьте ключ, модель и доступный лимит."
            ) from None
        self.record_test(profile, "connected")
        return {"ok": True, "message": "Провайдер выполнил тестовый запрос."}

    def codex_pool(self):
        if self.codex is None:
            from standalone_agent.codex_provider import CodexPool

            self.codex = CodexPool(root=self.store.path.parent / "codex-accounts")
        return self.codex

    async def codex_status(self, owner_id):
        try:
            status = await self.codex_pool().status(owner_id)
            fields = ("installed", "connected", "login_pending")
            if any(type(status[key]) is not bool for key in fields):
                raise ValueError("Invalid Codex status")
            return {key: status[key] for key in fields}
        except ConversationError as exc:
            error = SettingsError(
                "Codex недоступен. Проверьте установку CLI.", "codex_unavailable", 503
            )
            error.installed = exc.code != "codex_unavailable"
            raise error from None
        except (ValueError, KeyError, TypeError):
            raise SettingsError(
                "Codex недоступен. Проверьте установку CLI.", "codex_unavailable", 503
            ) from None

    async def codex_login(self, owner_id):
        try:
            result = await self.codex_pool().login(owner_id)
            url, code, login_id = (
                result[key] for key in ("verification_url", "user_code", "login_id")
            )
            if not all(isinstance(value, str) and value for value in (url, code, login_id)):
                raise ValueError("Invalid login response")
            parsed = urlsplit(url)
            if (
                parsed.scheme != "https"
                or parsed.hostname != "auth.openai.com"
                or parsed.port not in (None, 443)
                or parsed.username is not None
                or not parsed.path.startswith("/")
                or parsed.fragment
                or len(url) > 2048
                or any(c.isspace() for c in url)
                or len(code) > 100
                or len(login_id) > 256
                or any(ord(c) < 32 or ord(c) > 126 for c in code + login_id)
            ):
                raise ValueError("Invalid official login response")
            return {"verification_url": url, "user_code": code, "login_id": login_id}
        except (ValueError, KeyError, TypeError):
            raise SettingsError(
                "Не удалось начать официальный вход Codex.", "codex_login_failed"
            ) from None

    async def codex_logout(self, owner_id):
        try:
            await self.codex_pool().logout(owner_id)
            return {"ok": True}
        except ValueError:
            raise SettingsError("Не удалось выйти из Codex.", "codex_logout_failed") from None

    async def close(self):
        if self.codex is not None:
            await self.codex.close()
