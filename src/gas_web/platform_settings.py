"""Browser-owner platform credentials; never part of model prompts or public profiles."""

import asyncio
import json
import os
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from types import SimpleNamespace

from iacpaas_mcp.config import backend_from_env
from iacpaas_mcp.domain import DomainError
from iacpaas_mcp.platform import IACPaaSBackend
from iacpaas_mcp.resource_config import (
    DEFAULT_RESOURCE_PATHS,
    RESOURCE_FIELDS,
    RESOURCE_LABELS,
    resource_paths_from_env,
    validate_resource_paths,
)
from standalone_agent.cli import connect
from standalone_agent.errors import ConversationError

from .model_settings import SettingsError


@dataclass(frozen=True)
class PlatformProfile:
    username: str = field(repr=False)
    password: str = field(repr=False)
    stored: bool = False
    resource_paths: dict = field(default_factory=dict)


class SafeSession:
    def __init__(self, session, profile):
        self.session, self.profile = session, profile

    async def list_tools(self):
        return await self.session.list_tools()

    async def call_tool(self, name, arguments):
        raw = await self.session.call_tool(name, arguments)

        def clean(value):
            if isinstance(value, str):
                for secret in (self.profile.password, self.profile.username):
                    if secret:
                        value = value.replace(secret, '[REDACTED]')
                return value
            if isinstance(value, list):
                return [clean(item) for item in value]
            if isinstance(value, dict):
                return {clean(key): clean(item) for key, item in value.items()}
            return value

        return SimpleNamespace(isError=raw.isError, structuredContent=clean(raw.structuredContent))


class PlatformSettings:
    def __init__(self, store):
        self.store = store
        with store.connect() as db:
            db.execute('''CREATE TABLE IF NOT EXISTS platform_profiles (
                owner_id TEXT PRIMARY KEY REFERENCES owners(id),
                username TEXT NOT NULL, password TEXT NOT NULL)''')
            if 'resource_paths' not in {row['name'] for row in db.execute('PRAGMA table_info(platform_profiles)')}:
                db.execute('ALTER TABLE platform_profiles ADD COLUMN resource_paths TEXT')

    @staticmethod
    def local():
        return os.environ.get('MCP_TRANSPORT', 'stdio') == 'stdio'

    def snapshot(self, owner_id):
        with self.store.connect() as db:
            row = db.execute('SELECT username,password,resource_paths FROM platform_profiles WHERE owner_id=?', (owner_id,)).fetchone()
        paths = json.loads(row['resource_paths']) if row and row['resource_paths'] is not None else resource_paths_from_env()
        return PlatformProfile(row['username'], row['password'], True, paths) if row else PlatformProfile(
            os.environ.get('IACPAAS_USERNAME', ''), os.environ.get('IACPAAS_PASSWORD', ''),
            resource_paths=paths)

    def public(self, owner_id):
        profile = self.snapshot(owner_id)
        return {'username': profile.username, 'password_set': bool(profile.password),
                'configured': bool(profile.username and profile.password),
                'source': 'saved' if profile.stored else 'environment', 'local_mcp': self.local(),
                'resource_paths': profile.resource_paths,
                'resource_fields': [{'key': key, 'label': label, 'default_path': DEFAULT_RESOURCE_PATHS[key]}
                                    for key, label in RESOURCE_LABELS.items()]}

    def save(self, owner_id, body):
        if not self.local():
            raise ValueError('Вход IACPaaS настраивается на удалённом MCP-сервере.')
        if not isinstance(body, dict) or set(body) - {'username', 'password', 'clear_password', 'resource_paths'}:
            raise ValueError('Некорректные поля входа IACPaaS.')
        username, password = body.get('username'), body.get('password', '')
        clear = body.get('clear_password', False)
        if (not isinstance(username, str) or not isinstance(password, str)
            or len(username) > 300 or len(password) > 4096 or type(clear) is not bool
            or any(ord(char) < 32 for char in username + password)):
            raise ValueError('Некорректный логин или пароль IACPaaS.')
        username = username.strip()
        previous = self.snapshot(owner_id)
        try:
            paths = validate_resource_paths(body.get('resource_paths', previous.resource_paths))
        except DomainError as exc:
            raise ValueError(str(exc)) from None
        if clear and password:
            raise ValueError('Нельзя одновременно удалить и задать пароль.')
        if username != previous.username and not password and not clear:
            raise ValueError('При смене логина введите пароль для нового пользователя.')
        password = '' if clear else password or previous.password
        if not username and password:
            raise ValueError('Укажите логин IACPaaS.')
        with self.store.connect() as db:
            db.execute('INSERT OR REPLACE INTO platform_profiles '
                       '(owner_id,username,password,resource_paths) VALUES (?,?,?,?)',
                       (owner_id, username, password, json.dumps(paths, ensure_ascii=False)))

    def session_factory(self, owner_id):
        profile = self.snapshot(owner_id)
        if self.local() and backend_from_env() == 'iacpaas' and not (profile.username and profile.password):
            raise ConversationError('Укажите логин и пароль IACPaaS.', 'platform_configuration_error')

        @asynccontextmanager
        async def session():
            transport = os.environ.get('MCP_TRANSPORT', 'stdio')
            overrides = {'IACPAAS_USERNAME': profile.username, 'IACPAAS_PASSWORD': profile.password,
                         'IACPAAS_TOKEN': '', 'IACPAAS_RESOURCE_PATHS_EXPLICIT': '1',
                         **{key: profile.resource_paths.get(key, '') for key in RESOURCE_FIELDS}
                         } if transport == 'stdio' else None
            async with connect(transport, os.environ.get('MCP_URL', 'http://127.0.0.1:8000/mcp'),
                               env_overrides=overrides) as raw:
                yield SafeSession(raw, profile)
        return session

    async def test(self, owner_id):
        if not self.local():
            raise SettingsError('Проверьте вход в настройках удалённого MCP-сервера.', status=400)
        if backend_from_env() != 'iacpaas':
            raise SettingsError('Сервер работает с демонстрационными данными. Включите IACPAAS_BACKEND=iacpaas.', status=400)
        profile = self.snapshot(owner_id)
        if not (profile.username and profile.password):
            raise SettingsError('Укажите и сохраните логин и пароль IACPaaS.',
                                code='platform_configuration_error', status=400)

        def read():
            backend = IACPaaSBackend.from_env(username=profile.username, password=profile.password,
                                             resources=profile.resource_paths)
            catalog = backend.catalog()
            failures = []
            for item in catalog:
                try:
                    backend.get(item['path'])
                except DomainError as exc:
                    if exc.code not in {'permission_denied', 'not_found', 'invalid_data', 'limit_exceeded'}:
                        raise
                    reason = {
                        'permission_denied': 'нет прав на чтение',
                        'not_found': 'ресурс не найден',
                        'invalid_data': 'неподдерживаемые данные ресурса',
                        'limit_exceeded': 'превышен лимит загрузки',
                    }[exc.code]
                    label = next((RESOURCE_LABELS[key] for key, path in profile.resource_paths.items()
                                  if path == item['path']), 'Ресурс')
                    failures.append(f'{label}: {reason}')
            if failures:
                raise SettingsError(f'Прочитано ресурсов: {len(catalog) - len(failures)} из {len(catalog)}. '
                                    + '; '.join(failures) + '. Проверьте пути в настройках и доступ в IACPaaS.',
                                    code='platform_resources_unavailable', status=400)
            return len(catalog)
        try:
            count = await asyncio.to_thread(read)
        except SettingsError:
            raise
        except DomainError as exc:
            errors = {
                'authentication_error': (
                    'IACPaaS отклонила вход. Проверьте логин и пароль.', 401),
                'permission_denied': (
                    'Вход выполнен, но IACPaaS не разрешает чтение настроенного ресурса. '
                    'Проверьте права доступа и путь ресурса в настройках.', 403),
                'not_found': (
                    'Настроенный ресурс не найден. Проверьте его путь в настройках '
                    'относительно «Моего фонда» этого пользователя.', 404),
                'timeout': ('IACPaaS не ответила вовремя. Проверьте соединение с платформой.', 504),
                'platform_unavailable': (
                    'Не удалось соединиться с IACPaaS. Проверьте сеть и доступность '
                    'https://iacpaas.dvo.ru с компьютера, где запущен сервер.', 502),
                'platform_error': ('IACPaaS сообщила об ошибке обработки запроса.', 502),
                'invalid_data': ('IACPaaS вернула неподдерживаемые данные ресурса.', 502),
                'limit_exceeded': (
                    'Ресурс превышает настроенный лимит загрузки IACPaaS_EXPORT_BYTES.', 413),
            }
            if exc.code == 'configuration_error':
                message = (
                    'Пути ресурсов IACPaaS не настроены. Укажите их в настройках '
                    'относительно «Моего фонда». Логин и пароль ещё не проверялись.'
                    if not any(profile.resource_paths.values())
                    else 'Некорректная конфигурация IACPaaS в .env. Проверьте адрес платформы, '
                    'пути ресурсов и числовые лимиты. Логин и пароль ещё не проверялись.'
                )
                raise SettingsError(message, code='platform_configuration_error', status=400) from None
            message, status = errors.get(exc.code, ('Не удалось прочитать ресурс IACPaaS.', 502))
            raise SettingsError(message, code='platform_' + exc.code, status=status) from None
        except Exception:
            # Neither upstream exceptions nor credentials cross the API boundary.
            raise SettingsError('Не удалось завершить проверку IACPaaS. '
                                'Это не подтверждает ошибку логина или пароля.',
                                code='platform_check_failed') from None
        return {'connected': True, 'resource_count': count,
                'message': f'Вход выполнен. Все настроенные ресурсы доступны для чтения: {count}.'}
