"""Resource and ontology configuration reaches both checks and the real MCP child."""

import asyncio
import json
from contextlib import asynccontextmanager
from pathlib import Path

import pytest
from dotenv import dotenv_values
from starlette.testclient import TestClient

from gas_web.app import create_app
from gas_web.model_settings import SettingsError
from gas_web.platform_settings import PlatformSettings
from gas_web.storage import Storage
from iacpaas_mcp.domain import DomainError
from iacpaas_mcp.platform import IACPaaSBackend
from iacpaas_mcp.resource_config import (
    DEFAULT_RESOURCE_PATHS,
    RESOURCE_FIELDS,
    resource_paths_from_env,
)


def test_defaults_include_eight_real_resource_paths_and_match_template(monkeypatch):
    for key in (*RESOURCE_FIELDS, 'IACPAAS_RESOURCE_PATHS_EXPLICIT'):
        monkeypatch.delenv(key, raising=False)
    paths = resource_paths_from_env()
    assert len([p for p in paths.values() if p]) == 8
    assert paths['IACPAAS_GASES_ONTOLOGY_PATH'] == 'Загрузки / Онтология базы технологических газов'
    assert paths['IACPAAS_SUBSTANCES_ONTOLOGY_PATH'] == ''
    backend = IACPaaSBackend.from_env(username='fixture', password='fixture')
    assert {x['path'] for x in backend.catalog()} == {p for p in paths.values() if p}
    template = dotenv_values(Path(__file__).resolve().parents[1] / '.env.example')
    assert all(template[key] == path for key, path in paths.items())
    monkeypatch.setenv('IACPAAS_GASES_PATH', 'Another folder / Gases')
    assert resource_paths_from_env()['IACPAAS_GASES_PATH'] == 'Another folder / Gases'


def test_old_credentials_migrate_and_browser_paths_survive_restart(tmp_path, monkeypatch):
    monkeypatch.setenv('MCP_TRANSPORT', 'stdio')
    storage = Storage(tmp_path / 'chat.db')
    owner, _ = storage.new_owner()
    other, _ = storage.new_owner()
    with storage.connect() as db:
        db.execute('CREATE TABLE platform_profiles (owner_id TEXT PRIMARY KEY, username TEXT, password TEXT)')
        db.execute('INSERT INTO platform_profiles VALUES (?,?,?)', (owner['id'], 'login', 'secret'))
    settings = PlatformSettings(storage)
    assert settings.snapshot(owner['id']).password == 'secret'
    assert settings.snapshot(owner['id']).resource_paths == resource_paths_from_env()
    paths = dict(DEFAULT_RESOURCE_PATHS, IACPAAS_GASES_PATH='My folder / Gas',
                 IACPAAS_TERMS_PATH='')
    settings.save(owner['id'], {'username': 'login', 'resource_paths': paths})
    restarted = PlatformSettings(Storage(tmp_path / 'chat.db'))
    profile = restarted.snapshot(owner['id'])
    assert profile.password == 'secret' and profile.resource_paths == paths
    assert restarted.snapshot(other['id']).resource_paths != paths
    assert 'secret' not in json.dumps(restarted.public(owner['id']))


@pytest.mark.parametrize('paths', [{}, {'UNKNOWN': 'Folder / Resource'},
    {'IACPAAS_GASES_PATH': '../Other'}, {'IACPAAS_GASES_PATH': '/absolute'},
    {'IACPAAS_GASES_PATH': 'folder\nresource'}, {'IACPAAS_GASES_PATH': 1}])
def test_invalid_paths_do_not_overwrite_saved_credentials_or_paths(tmp_path, monkeypatch, paths):
    monkeypatch.setenv('MCP_TRANSPORT', 'stdio')
    settings = PlatformSettings(Storage(tmp_path / 'chat.db'))
    owner, _ = settings.store.new_owner()
    settings.save(owner['id'], {'username': 'login', 'password': 'secret'})
    before = settings.snapshot(owner['id'])
    with pytest.raises(ValueError):
        settings.save(owner['id'], {'username': 'login', 'password': 'replacement',
                                   'resource_paths': paths})
    assert settings.snapshot(owner['id']) == before


def test_child_receives_only_own_paths_including_explicit_disabled_defaults(tmp_path, monkeypatch):
    import gas_web.platform_settings as module

    monkeypatch.setenv('MCP_TRANSPORT', 'stdio')
    monkeypatch.setenv('IACPAAS_BACKEND', 'iacpaas')
    settings = PlatformSettings(Storage(tmp_path / 'chat.db'))
    owner, _ = settings.store.new_owner()
    paths = {key: '' for key in RESOURCE_FIELDS}
    paths.update(IACPAAS_GASES_PATH='Private / Gas', IACPAAS_GASES_ONTOLOGY_PATH='Private / Ontology')
    settings.save(owner['id'], {'username': 'login', 'password': 'secret', 'resource_paths': paths})
    captured = []

    @asynccontextmanager
    async def connect(*args, **kwargs):
        captured.append(kwargs['env_overrides'])
        yield object()

    monkeypatch.setattr(module, 'connect', connect)

    async def check():
        async with settings.session_factory(owner['id'])():
            pass

    asyncio.run(check())
    for key, value in captured[0].items():
        monkeypatch.setenv(key, value)
    backend = IACPaaSBackend.from_env()
    assert {x['path'] for x in backend.catalog()} == {'Private / Gas', 'Private / Ontology'}


def test_access_check_reads_base_and_ontology_and_reports_partial_failure(tmp_path, monkeypatch):
    import gas_web.platform_settings as module

    monkeypatch.setenv('MCP_TRANSPORT', 'stdio')
    monkeypatch.setenv('IACPAAS_BACKEND', 'iacpaas')
    paths = {'IACPAAS_GASES_PATH': 'Custom / Gas',
             'IACPAAS_GASES_ONTOLOGY_PATH': 'Custom / Ontology'}
    seen = []

    class Backend:
        @classmethod
        def from_env(cls, **kwargs):
            assert kwargs['resources']['IACPAAS_GASES_PATH'] == 'Custom / Gas'
            return cls()

        def catalog(self):
            return [{'path': path} for path in paths.values()]

        def get(self, path):
            seen.append(path)
            if path.endswith('Ontology'):
                raise DomainError('not_found', 'secret upstream body')

    monkeypatch.setattr(module, 'IACPaaSBackend', Backend)
    settings = PlatformSettings(Storage(tmp_path / 'chat.db'))
    owner, _ = settings.store.new_owner()
    settings.save(owner['id'], {'username': 'login', 'password': 'secret', 'resource_paths': paths})
    with pytest.raises(SettingsError) as failure:
        asyncio.run(settings.test(owner['id']))
    assert seen == list(paths.values())
    assert '1 из 2' in str(failure.value)
    assert 'Онтология базы технологических газов' in str(failure.value)
    assert 'secret' not in str(failure.value)


def test_public_form_contains_all_resource_fields(tmp_path, monkeypatch):
    monkeypatch.setenv('MCP_TRANSPORT', 'stdio')
    with TestClient(create_app(db_path=tmp_path / 'chat.db'), base_url='http://127.0.0.1') as client:
        client.get('/api/me')
        public = client.get('/api/platform/settings').json()
        html = client.get('/').text
        assert set(public['resource_paths']) == set(RESOURCE_FIELDS)
        assert all(f'data-resource-key="{key}"' in html for key in RESOURCE_FIELDS)
