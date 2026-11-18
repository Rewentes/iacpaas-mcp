"""Shared editable resource paths for standalone MCP and browser profiles."""

import os

from .domain import DomainError

RESOURCE_LABELS = {
    "IACPAAS_GASES_PATH": "База технологических газов",
    "IACPAAS_GASES_ONTOLOGY_PATH": "Онтология базы технологических газов",
    "IACPAAS_SUBSTANCES_PATH": "База химических элементов и веществ",
    "IACPAAS_SUBSTANCES_ONTOLOGY_PATH": "Онтология химических элементов и веществ (если отдельная)",
    "IACPAAS_UNITS_PATH": "Система единиц физических величин",
    "IACPAAS_CHARACTERISTICS_ONTOLOGY_PATH": "Онтология описания характеристик и их значений",
    "IACPAAS_PROPERTIES_PATH": "База свойств материалов",
    "IACPAAS_PROPERTIES_ONTOLOGY_PATH": "Онтология базы свойств материалов",
    "IACPAAS_TERMS_PATH": "Терминологический справочник",
}
RESOURCE_FIELDS = tuple(RESOURCE_LABELS)
DEFAULT_RESOURCE_PATHS = {
    key: "Загрузки / " + label if key != "IACPAAS_SUBSTANCES_ONTOLOGY_PATH" else ""
    for key, label in RESOURCE_LABELS.items()
}


def resource_paths_from_env():
    explicit = os.environ.get('IACPAAS_RESOURCE_PATHS_EXPLICIT') == '1'
    return {
        key: (os.environ.get(key, "").strip() if explicit
              else os.environ.get(key, "").strip() or DEFAULT_RESOURCE_PATHS[key])
        for key in RESOURCE_FIELDS
    }


def validate_resource_paths(paths):
    if not isinstance(paths, dict) or set(paths) - set(RESOURCE_FIELDS):
        raise DomainError("configuration_error", "Некорректные поля путей ресурсов IACPaaS.")
    result = {}
    for key in RESOURCE_FIELDS:
        path = paths.get(key, "")
        if not isinstance(path, str) or len(path) > 1024 or any(ord(c) < 32 for c in path):
            raise DomainError("configuration_error", "Некорректный путь ресурса IACPaaS.")
        path = path.strip()
        if path and any(part.strip() in ("", ".", "..") for part in path.split("/")):
            raise DomainError("configuration_error", "Укажите путь относительно «Моего фонда».")
        result[key] = path
    if not any(result.values()):
        raise DomainError("configuration_error", "Укажите хотя бы один ресурс IACPaaS.")
    return result
