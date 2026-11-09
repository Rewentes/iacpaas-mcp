"""Bounded conversion of IACPaaS universal exports, without fetching references."""

import json
from typing import Annotated, Literal

from pydantic import Field, ValidationError, model_validator

from .domain import DomainError, _Edge, _Node, _Strict


class _Reference(_Strict):
    kind: Literal["clone", "link"]
    target_id: Annotated[str, Field(min_length=1, max_length=1024)] | None = None
    resource_path: Annotated[str, Field(max_length=1024)] | None = None
    concept_path: Annotated[str, Field(max_length=4096)] | None = None


class _NativeNode(_Node):
    node_type: Literal["КОРЕНЬ", "НЕТЕРМИНАЛ", "ТЕРМИНАЛ-СОРТ", "ТЕРМИНАЛ-ЗНАЧЕНИЕ"]
    value_type: Literal["STRING", "INTEGER", "REAL", "BOOLEAN", "DATE", "BLOB"] | None = None
    reference: _Reference | None = None

    @model_validator(mode="after")
    def check_value_type(self):
        if "value" not in self.model_fields_set:
            return self
        valid = {
            "STRING": isinstance(self.value, str),
            "DATE": isinstance(self.value, str),
            "BLOB": isinstance(self.value, str),
            "INTEGER": type(self.value) is int,
            "BOOLEAN": type(self.value) is bool,
            "REAL": type(self.value) in (int, float)
            or isinstance(self.value, str)
            and self.value in {"NaN", "Infinity", "-Infinity"},
        }
        if not valid.get(self.value_type, False):
            raise ValueError("Value does not match its IACPaaS type")
        return self


class _NativeEdge(_Edge):
    specifier: Annotated[str, Field(max_length=128)] | None = None
    restrictor: Annotated[str, Field(max_length=128)] | None = None
    comment: Annotated[str, Field(max_length=16384)] | None = None


class _Source(_Strict):
    kind: Literal["iacpaas_export"]
    fetched_at: Annotated[str, Field(max_length=128)]
    platform_modified_at: Annotated[str, Field(max_length=128)] | None = None


class _NativeGraph(_Strict):
    format: Literal["normalized-iacpaas-v1"]
    mock: Literal[False]
    path: Annotated[str, Field(min_length=1, max_length=1024)]
    name: Annotated[str, Field(min_length=1, max_length=4096)]
    root: Annotated[str, Field(min_length=1, max_length=1024)]
    ontology: Annotated[str, Field(max_length=1024)] | None = None
    nodes: Annotated[list[_NativeNode], Field(min_length=1, max_length=50000)]
    edges: Annotated[list[_NativeEdge], Field(max_length=200000)]
    source: _Source

    @model_validator(mode="after")
    def check_graph(self):
        identifiers = {node.id for node in self.nodes}
        if len(identifiers) != len(self.nodes) or self.root not in identifiers:
            raise ValueError("Invalid vertex identities")
        if any(
            edge.source not in identifiers or edge.target not in identifiers for edge in self.edges
        ):
            raise ValueError("Invalid graph edge")
        return self


def validate_native_graph(payload):
    try:
        json.dumps(payload, allow_nan=False)
        return _NativeGraph.model_validate(payload).model_dump(mode="json", exclude_unset=True)
    except (ValidationError, TypeError, ValueError, RecursionError):
        raise DomainError("invalid_data", "Некорректный нормализованный граф IACPaaS.") from None


def platform_path(value):
    """Keep resource-relative identifiers; omit account identifiers from model data."""
    if not isinstance(value, str):
        return None
    parts = [part.strip() for part in value.split("/")]
    if "Мой Фонд" in parts:
        parts = parts[parts.index("Мой Фонд") + 1 :]
    return " / ".join(parts).rstrip("$;").strip()


def _identifier(value):
    if type(value) is not int or value <= 0:
        raise DomainError(
            "invalid_data", "В universal отсутствует корректный идентификатор вершины."
        )
    return str(value)


def _reference(raw):
    for key, kind in (("link", "link"), ("original", "clone")):
        if key not in raw and key + "_id" not in raw:
            continue
        reference = {"kind": kind}
        if key + "_id" in raw:
            reference["target_id"] = _identifier(raw[key + "_id"])
        if key in raw:
            if not isinstance(raw[key], str) or len(raw[key]) > 8192:
                raise DomainError("invalid_data", "Некорректная ссылка universal.")
            resource, separator, concept = raw[key].partition("$")
            reference["resource_path"] = platform_path(resource)
            if separator:
                reference["concept_path"] = concept.rstrip(";").strip()
        return reference
    return None


def normalize_universal(payload, *, path, fetched_at):
    if (
        not isinstance(payload, dict)
        or payload.get("json_type") != "universal"
        or payload.get("type") != "КОРЕНЬ"
        or not isinstance(payload.get("title"), str)
    ):
        raise DomainError("invalid_data", "Ожидался полный universal-экспорт IACPaaS.")
    nodes, edges, expanded = {}, [], set()
    stack = [(payload, None, 0)]
    occurrences = 0
    while stack:
        raw, parent, depth = stack.pop()
        occurrences += 1
        if depth > 128 or occurrences > 200000:
            raise DomainError("limit_exceeded", "Превышен предел структуры universal.")
        if not isinstance(raw, dict):
            raise DomainError("invalid_data", "Некорректная вершина universal.")
        identifier = _identifier(raw.get("id"))
        if parent is not None:
            edge = {"source": parent, "target": identifier, "relation": raw.get("meta", "")}
            for source, target in (
                ("specifier", "specifier"),
                ("restrictor", "restrictor"),
                ("rcomment", "comment"),
            ):
                if source in raw:
                    edge[target] = raw[source]
            edges.append(edge)
        children = raw.get("successors", [])
        if not isinstance(children, list):
            raise DomainError("invalid_data", "successors должен быть массивом.")
        node = {
            "id": identifier,
            "name": raw.get("name", str(raw.get("value", raw.get("meta", "")))),
            "metatype": raw.get("meta", ""),
            "node_type": raw.get("type", ""),
        }
        if "value" in raw:
            node["value"] = raw["value"]
        if "valtype" in raw:
            node["value_type"] = raw["valtype"]
        if "comment" in raw:
            node["comment"] = raw["comment"]
        reference = _reference(raw)
        if reference:
            node["reference"] = reference
        # A repeated link names an existing vertex; it does not replace its definition.
        if identifier not in nodes or "link" not in raw and "link_id" not in raw:
            if identifier in expanded and children:
                raise DomainError("invalid_data", "Повторное определение вершины universal.")
            nodes[identifier] = node
        if len(nodes) > 50000:
            raise DomainError("limit_exceeded", "Ресурс превышает предел 50000 вершин.")
        if children:
            expanded.add(identifier)
            stack.extend((child, identifier, depth + 1) for child in reversed(children))
    graph = {
        "format": "normalized-iacpaas-v1",
        "mock": False,
        "path": path,
        "name": payload["title"],
        "root": _identifier(payload.get("id")),
        "ontology": platform_path(payload.get("ontology")),
        "nodes": list(nodes.values()),
        "edges": edges,
        "source": {
            "kind": "iacpaas_export",
            "fetched_at": fetched_at,
            "platform_modified_at": payload.get("date"),
        },
    }
    return validate_native_graph(graph)
