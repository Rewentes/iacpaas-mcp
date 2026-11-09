"""Read-only IACPaaS HTTP adapter with private JWT and bounded exports."""

import json
import os
import threading
import time
from copy import deepcopy
from datetime import datetime, timezone

import httpx

from .domain import DomainError
from .native import normalize_universal

RESOURCE_FIELDS = (
    "IACPAAS_GASES_PATH",
    "IACPAAS_GASES_ONTOLOGY_PATH",
    "IACPAAS_SUBSTANCES_PATH",
    "IACPAAS_SUBSTANCES_ONTOLOGY_PATH",
    "IACPAAS_UNITS_PATH",
    "IACPAAS_CHARACTERISTICS_ONTOLOGY_PATH",
    "IACPAAS_PROPERTIES_PATH",
    "IACPAAS_PROPERTIES_ONTOLOGY_PATH",
    "IACPAAS_TERMS_PATH",
)


class IACPaaSBackend:
    kind = "iacpaas"

    def __init__(
        self,
        *,
        username,
        password,
        resources,
        base_url="https://iacpaas.dvo.ru",
        transport=None,
        max_export_bytes=16 * 1024 * 1024,
        cache_seconds=60,
    ):
        if base_url.rstrip("/") != "https://iacpaas.dvo.ru":
            raise DomainError("configuration_error", "Разрешён только HTTPS-адрес IACPaaS.")
        if not username or not password:
            raise DomainError("configuration_error", "Укажите логин и пароль IACPaaS.")
        paths = list(dict.fromkeys(resources.values()))
        if (
            not paths
            or len(paths) > 16
            or any(
                not isinstance(path, str)
                or not 1 <= len(path) <= 1024
                or any(ord(char) < 32 for char in path)
                or any(part.strip() in ("", ".", "..") for part in path.split("/"))
                for path in paths
            )
        ):
            raise DomainError("configuration_error", "Укажите допустимые пути ресурсов IACPaaS.")
        if type(max_export_bytes) is not int or not 1024 <= max_export_bytes <= 64 * 1024 * 1024:
            raise DomainError("configuration_error", "Некорректный лимит экспорта IACPaaS.")
        if type(cache_seconds) is not int or not 0 <= cache_seconds <= 300:
            raise DomainError("configuration_error", "Некорректный срок кэша IACPaaS.")
        self._username, self._password = username, password
        self._token = None
        self._transport = transport
        self._paths = paths
        self._max_export_bytes = max_export_bytes
        self._cache_seconds = cache_seconds
        self._cache = {}
        self._lock = threading.RLock()

    @classmethod
    def from_env(cls, *, username=None, password=None):
        try:
            return cls(
                username=os.environ.get("IACPAAS_USERNAME", "") if username is None else username,
                password=os.environ.get("IACPAAS_PASSWORD", "") if password is None else password,
                resources={
                    field: os.environ[field].strip()
                    for field in RESOURCE_FIELDS
                    if os.environ.get(field, "").strip()
                },
                base_url=os.environ.get("IACPAAS_BASE_URL", "https://iacpaas.dvo.ru"),
                max_export_bytes=int(os.environ.get("IACPAAS_EXPORT_BYTES", str(16 * 1024 * 1024))),
                cache_seconds=int(os.environ.get("IACPAAS_CACHE_SECONDS", "60")),
            )
        except ValueError:
            raise DomainError("configuration_error", "Некорректная конфигурация IACPaaS.") from None

    def catalog(self):
        return [{"path": path, "name": path.rsplit("/", 1)[-1].strip()} for path in self._paths]

    def _request(self, method, endpoint, *, limit, **kwargs):
        try:
            with httpx.Client(
                transport=self._transport,
                timeout=httpx.Timeout(60, connect=30),
                follow_redirects=False,
                trust_env=False,
            ) as client:
                with client.stream(
                    method, "https://iacpaas.dvo.ru" + endpoint, **kwargs
                ) as response:
                    status = response.status_code
                    if status in (401, 403):
                        return status, None
                    if status == 404:
                        raise DomainError("not_found", "Ресурс IACPaaS не найден.")
                    if status != 200:
                        raise DomainError(
                            "platform_error", "Платформа IACPaaS отклонила запрос.", status >= 500
                        )
                    chunks = bytearray()
                    content_length = response.headers.get("content-length")
                    if content_length and content_length.isdigit() and int(content_length) > limit:
                        raise DomainError(
                            "limit_exceeded", "Экспорт IACPaaS превышает лимит загрузки."
                        )
                    for chunk in response.iter_bytes():
                        if len(chunks) + len(chunk) > limit:
                            raise DomainError(
                                "limit_exceeded", "Экспорт IACPaaS превышает лимит загрузки."
                            )
                        chunks.extend(chunk)
            data = json.loads(chunks, parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))
            if not isinstance(data, dict):
                raise ValueError
            if data.get("success") is False or data.get("platformUnavailable") is True:
                raise DomainError(
                    "platform_error", "Платформа IACPaaS сообщила об ошибке операции."
                )
            return status, data
        except DomainError:
            raise
        except httpx.TimeoutException:
            raise DomainError("timeout", "Превышено время ожидания IACPaaS.", True) from None
        except httpx.HTTPError:
            raise DomainError(
                "platform_unavailable", "Не удалось подключиться к IACPaaS.", True
            ) from None
        except (ValueError, TypeError, RecursionError):
            raise DomainError("invalid_data", "IACPaaS вернула некорректный JSON.") from None

    def _login(self):
        self._token = None
        status, data = self._request(
            "POST",
            "/api/signin",
            limit=65536,
            json={"username": self._username, "password": self._password},
        )
        token = data.get("accessToken") if data else None
        if status != 200 or not isinstance(token, str) or not token or len(token) > 32768:
            raise DomainError(
                "authentication_error", "Не удалось войти в IACPaaS: проверьте логин и пароль."
            )
        self._token = token

    def get(self, path):
        if path not in self._paths:
            raise DomainError("permission_denied", "Ресурс не входит в настроенный список IACPaaS.")
        with self._lock:
            cached = self._cache.get(path)
            if cached and time.monotonic() - cached[0] < self._cache_seconds:
                return deepcopy(cached[1])
            if self._token is None:
                self._login()
            params = {"path": path, "json-type": "universal", "no-blob-data": "true"}
            status, data = self._request(
                "GET",
                "/api/data/export/user-item",
                limit=self._max_export_bytes,
                headers={"Authorization": "Bearer " + self._token},
                params=params,
            )
            # Only a read with a rejected JWT may be repeated, once, after a fresh login.
            if status == 401:
                self._login()
                status, data = self._request(
                    "GET",
                    "/api/data/export/user-item",
                    limit=self._max_export_bytes,
                    headers={"Authorization": "Bearer " + self._token},
                    params=params,
                )
            if status in (401, 403):
                raise DomainError("permission_denied", "IACPaaS не разрешает чтение этого ресурса.")
            payload = data.get("data", data.get("result", data))
            if isinstance(payload, str):
                try:
                    payload = json.loads(
                        payload, parse_constant=lambda _: (_ for _ in ()).throw(ValueError())
                    )
                except (ValueError, RecursionError):
                    raise DomainError(
                        "invalid_data", "Некорректный universal-экспорт IACPaaS."
                    ) from None
            if (
                not isinstance(payload, dict)
                or payload.get("title") != path.rsplit("/", 1)[-1].strip()
            ):
                raise DomainError(
                    "invalid_data", "Ответ IACPaaS не соответствует запрошенному ресурсу."
                )
            graph = normalize_universal(
                payload,
                path=path,
                fetched_at=datetime.now(timezone.utc).isoformat(),
            )
            self._cache[path] = (time.monotonic(), graph)
            return deepcopy(graph)

    def save(self, resource):
        raise DomainError("permission_denied", "Изменение реальных ресурсов IACPaaS отключено.")
