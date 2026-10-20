"""Операции над локальным нормализованным учебным графом, не API IACPaaS."""

import json
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, JsonValue, ValidationError, model_validator

from .adapters import Backend, MockBackend

Text = Annotated[str, Field(min_length=1, max_length=4096)]


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class _Node(_Strict):
    id: Text
    name: Text
    metatype: Text
    value: JsonValue = None
    comment: Annotated[str, Field(max_length=16384)] = ""
    mark: Annotated[str, Field(max_length=4096)] | None = None


class _Edge(_Strict):
    source: Text
    target: Text
    relation: Text


class _Resource(_Strict):
    format: Literal["normalized-demo-v1"]
    mock: Literal[True]
    path: Annotated[str, Field(min_length=2, max_length=1024)]
    name: Text
    nodes: Annotated[list[_Node], Field(min_length=1, max_length=2000)]
    edges: Annotated[list[_Edge], Field(max_length=10000)]

    @model_validator(mode="after")
    def check_graph(self):
        if (
            not self.path.startswith("/")
            or any(part in ("", ".", "..") for part in self.path[1:].split("/"))
            or any(char.isspace() or ord(char) < 32 for char in self.path)
        ):
            raise ValueError("Требуется абсолютный нормализованный путь")
        ids = {node.id for node in self.nodes}
        if len(ids) != len(self.nodes):
            raise ValueError("Идентификаторы вершин должны быть уникальны")
        if any(edge.source not in ids for edge in self.edges):
            raise ValueError("Источник ребра должен быть локальной вершиной")
        return self


class DomainError(Exception):
    def __init__(self, code: str, message: str, retryable: bool = False):
        super().__init__(message)
        self.code = code
        self.message = message
        self.retryable = retryable


class GraphService:
    def __init__(self, backend: Backend | None = None):
        if isinstance(backend, str):
            if backend != "mock":
                raise DomainError("unsupported_backend", "Реальный API IACPaaS не подключен")
            backend = None
        self.backend = backend if backend is not None else MockBackend()

    @staticmethod
    def _bound(value: int, low: int, high: int, name: str) -> None:
        if type(value) is not int or not low <= value <= high:
            raise DomainError("limit_exceeded", f"{name}: требуется целое {low}..{high}")

    def query_graph(
        self, path: str, field: str = "name", value: str = "", limit: int = 100
    ) -> dict:
        self._bound(limit, 1, 2000, "limit")
        if field not in {"id", "name", "metatype", "value", "comment", "mark"}:
            raise DomainError("invalid_data", "Неизвестное поле поиска")
        if not isinstance(value, str):
            raise DomainError("invalid_data", "value должен быть строкой")
        resource = self._get(path)
        matches = [
            node
            for node in resource["nodes"]
            if value.casefold() in str(node.get(field, "")).casefold()
        ]
        return {
            "path": path,
            "nodes": matches[:limit],
            "total_matches": len(matches),
            "complete": len(matches) <= limit,
            "scope": "local_resource_query",
            "mock": True,
            "warnings": [] if len(matches) <= limit else ["Результат поиска ограничен limit."],
        }

    def export_inforesource(self, path: str) -> dict:
        return self._normalize(self._get(path))

    def import_inforesource(self, payload: dict, clear_if_exists: bool = False) -> dict:
        if type(clear_if_exists) is not bool:
            raise DomainError("invalid_data", "clear_if_exists должен быть boolean")
        resource = self._normalize(payload)
        exists = any(item["path"] == resource["path"] for item in self.backend.catalog())
        if exists and not clear_if_exists:
            raise DomainError("invalid_data", "Ресурс существует; требуется clear_if_exists=true")
        self.backend.save(resource)
        return {
            "path": resource["path"],
            "replaced": exists,
            "mock": True,
            "complete": True,
            "scope": "volatile_mock_write",
            "warnings": ["Изменение только в памяти mock-процесса."],
        }

    def update_concept(
        self, path: str, concept_id: str, mark: str | None = None, comment: str | None = None
    ) -> dict:
        resource = self._get(path)
        for node in resource["nodes"]:
            if node["id"] == concept_id:
                if mark is not None:
                    node["mark"] = mark
                if comment is not None:
                    node["comment"] = comment
                self.backend.save(self._normalize(resource))
                return {
                    "path": path,
                    "concept": node,
                    "mock": True,
                    "complete": True,
                    "scope": "volatile_mock_write",
                    "warnings": ["Изменение только в памяти mock-процесса."],
                }
        raise DomainError("not_found", f"Вершина не найдена: {concept_id}")

    def _normalize(self, payload: dict) -> dict:
        try:
            json.dumps(payload, allow_nan=False)
            return _Resource.model_validate(payload).model_dump(mode="json", exclude_unset=True)
        except (ValidationError, TypeError, ValueError, RecursionError) as exc:
            raise DomainError("invalid_data", f"Неверный normalized-demo-v1: {exc}") from exc

    def validate_resource(self, payload: dict) -> dict:
        resource = self._normalize(payload)
        return {
            "valid": True,
            "format": resource["format"],
            "mock": True,
            "node_count": len(resource["nodes"]),
            "edge_count": len(resource["edges"]),
            "complete": True,
            "scope": "normalized_schema",
            "warnings": [],
        }

    def _get(self, path: str) -> dict:
        try:
            return self._normalize(self.backend.get(path))
        except KeyError as exc:
            raise DomainError("not_found", f"Ресурс не найден: {path}") from exc

    def get_subgraph(
        self, path: str, root: str = "root", depth: int = 2, max_nodes: int = 2000
    ) -> dict:
        self._bound(depth, 0, 5, "depth")
        self._bound(max_nodes, 1, 2000, "max_nodes")
        return self._traverse(self._get(path), root, depth, max_nodes)

    def _traverse(self, resource: dict, root: str, depth: int | None, max_nodes: int) -> dict:
        from collections import deque

        nodes = {node["id"]: node for node in resource["nodes"]}
        if root not in nodes:
            raise DomainError("not_found", f"Вершина не найдена: {root}")
        outgoing = {}
        for edge in resource["edges"]:
            outgoing.setdefault(edge["source"], []).append(edge["target"])
        selected = {root}
        ordered = [nodes[root]]
        queue = deque([(root, 0)])
        truncated = False
        while queue:
            source, level = queue.popleft()
            if depth is not None and level >= depth:
                continue
            for target in outgoing.get(source, []):
                if target not in nodes or target in selected:
                    continue
                if len(selected) >= max_nodes:
                    truncated = True
                    continue
                selected.add(target)
                ordered.append(nodes[target])
                queue.append((target, level + 1))
        edges = [
            edge
            for edge in resource["edges"]
            if edge["source"] in selected
            and (edge["target"] in selected or edge["target"] not in nodes)
        ]
        warnings = ["Достигнут лимит вершин."] if truncated else []
        if any(edge["target"] not in nodes for edge in edges):
            warnings.append("Внешние цели не загружаются; экземпляры ребер учтены.")
        return {
            "path": resource["path"],
            "root": root,
            "depth": depth,
            "nodes": ordered,
            "edges": edges,
            "complete": not truncated,
            "scope": "local_reachable_depth",
            "warnings": warnings,
            "mock": True,
        }

    def count_graph(self, path: str, root: str | None = None, depth: int | None = None) -> dict:
        if depth is not None:
            self._bound(depth, 0, 5, "depth")
        resource = self._get(path)
        graph = (
            self._traverse(resource, root or "root", depth, 2000)
            if root is not None or depth is not None
            else {**resource, "complete": True, "scope": "local_resource", "warnings": []}
        )
        local_ids = {node["id"] for node in resource["nodes"]}
        external = sum(edge["target"] not in local_ids for edge in graph["edges"])
        return {
            "path": path,
            "vertices": len(graph["nodes"]),
            "edges": len(graph["edges"]),
            "node_count": len(graph["nodes"]),
            "edge_count": len(graph["edges"]),
            "external_edge_count": external,
            "complete": graph["complete"],
            "scope": graph["scope"],
            "warnings": graph["warnings"],
            "mock": True,
        }

    def list_inforesources(self, filter: str = "", limit: int = 100) -> dict:
        self._bound(limit, 1, 2000, "limit")
        items = [
            item
            for item in self.backend.catalog()
            if filter.casefold() in (item["path"] + item["name"]).casefold()
        ]
        return {
            "resources": items[:limit],
            "complete": False,
            "scope": "configured_mock_catalog",
            "mock": True,
            "warnings": ["Каталог ограничен настроенными mock-ресурсами."],
        }

    def get_inforesource(self, path: str) -> dict:
        resource = self._get(path)
        return {
            "path": resource["path"],
            "name": resource["name"],
            "format": resource["format"],
            "mock": True,
            "node_count": len(resource["nodes"]),
            "edge_count": len(resource["edges"]),
            "complete": True,
            "scope": "local_resource",
            "warnings": [],
        }
