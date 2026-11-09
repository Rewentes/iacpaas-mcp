"""Browser-owner platform credentials; never part of model prompts or public profiles."""

import asyncio
import os
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from types import SimpleNamespace

from iacpaas_mcp.platform import IACPaaSBackend
from standalone_agent.cli import connect

from .model_settings import SettingsError


@dataclass(frozen=True)
class PlatformProfile:
    username: str = field(repr=False)
    password: str = field(repr=False)
    stored: bool = False


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

    @staticmethod
    def local():
        return os.environ.get('MCP_TRANSPORT', 'stdio') == 'stdio'

    def snapshot(self, owner_id):
        with self.store.connect() as db:
            row = db.execute('SELECT username,password FROM platform_profiles WHERE owner_id=?', (owner_id,)).fetchone()
        return PlatformProfile(row['username'], row['password'], True) if row else PlatformProfile(
            os.environ.get('IACPAAS_USERNAME', ''), os.environ.get('IACPAAS_PASSWORD', ''))

    def public(self, owner_id):
        profile = self.snapshot(owner_id)
        return {'username': profile.username, 'password_set': bool(profile.password),
                'configured': bool(profile.username and profile.password),
                'source': 'saved' if profile.stored else 'environment', 'local_mcp': self.local()}

    def save(self, owner_id, body):
        if not self.local():
            raise ValueError('Вход IACPaaS настраивается на удалённом MCP-сервере.')
        if not isinstance(body, dict) or set(body) - {'username', 'password', 'clear_password'}:
            raise ValueError('Некорректные поля входа IACPaaS.')
        username, password = body.get('username'), body.get('password', '')
        clear = body.get('clear_password', False)
        if (not isinstance(username, str) or not isinstance(password, str)
            or len(username) > 300 or len(password) > 4096 or type(clear) is not bool
            or any(ord(char) < 32 for char in username + password)):
            raise ValueError('Некорректный логин или пароль IACPaaS.')
        username = username.strip()
        previous = self.snapshot(owner_id)
        if clear and password:
            raise ValueError('Нельзя одновременно удалить и задать пароль.')
        if username != previous.username and not password and not clear:
            raise ValueError('При смене логина введите пароль для нового пользователя.')
        password = '' if clear else password or previous.password
        if not username and password:
            raise ValueError('Укажите логин IACPaaS.')
        with self.store.connect() as db:
            db.execute('INSERT OR REPLACE INTO platform_profiles VALUES (?,?,?)', (owner_id, username, password))

    def session_factory(self, owner_id):
        profile = self.snapshot(owner_id)

        @asynccontextmanager
        async def session():
            transport = os.environ.get('MCP_TRANSPORT', 'stdio')
            overrides = {'IACPAAS_USERNAME': profile.username, 'IACPAAS_PASSWORD': profile.password,
                         'IACPAAS_TOKEN': ''} if transport == 'stdio' else None
            async with connect(transport, os.environ.get('MCP_URL', 'http://127.0.0.1:8000/mcp'),
                               env_overrides=overrides) as raw:
                yield SafeSession(raw, profile)
        return session

    async def test(self, owner_id):
        if not self.local():
            raise SettingsError('Проверьте вход в настройках удалённого MCP-сервера.', status=400)
        if os.environ.get('IACPAAS_BACKEND', 'mock') != 'iacpaas':
            raise SettingsError('Сервер работает с демонстрационными данными. Включите IACPAAS_BACKEND=iacpaas.', status=400)
        profile = self.snapshot(owner_id)

        def read():
            backend = IACPaaSBackend.from_env(username=profile.username, password=profile.password)
            backend.get(backend.catalog()[0]['path'])
        try:
            await asyncio.to_thread(read)
        except Exception:
            # Neither upstream exceptions nor credentials cross the API boundary.
            raise SettingsError('Не удалось войти или прочитать ресурс. Проверьте логин, пароль и права доступа.', code='platform_unavailable') from None
        return {'connected': True, 'message': 'Вход выполнен, чтение ресурса IACPaaS доступно.'}
