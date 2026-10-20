"""Стабильный порт хранилища и исключительно локальная mock-реализация."""

import json
from copy import deepcopy
from pathlib import Path
from typing import Protocol


class Backend(Protocol):
    def catalog(self) -> list[dict]: ...

    def get(self, path: str) -> dict: ...

    def save(self, resource: dict) -> None: ...


class MockBackend:
    """Изолированная изменяемая копия fixture; никакой сети или fallback."""

    def __init__(self, fixture_path: str | Path | None = None):
        path = Path(fixture_path) if fixture_path else Path(__file__).parent / "data/demo.json"
        resource = json.loads(path.read_text(encoding="utf-8"))
        self._resources = {resource["path"]: deepcopy(resource)}

    def catalog(self) -> list[dict]:
        return [{"path": item["path"], "name": item["name"]} for item in self._resources.values()]

    def get(self, path: str) -> dict:
        return deepcopy(self._resources[path])

    def save(self, resource: dict) -> None:
        self._resources[resource["path"]] = deepcopy(resource)
