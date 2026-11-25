# Контракт MCP-инструментов

Оба транспорта используют один контракт. Снимки `tools/list`: [mock](tool-schemas.json), [IACPaaS](tool-schemas-iacpaas.json). Это схемы MCP на SDK, закреплённом в `pyproject.toml`, а не REST API платформы. Их соответствие текущему серверу проверяется автоматически.

| Инструмент | Аргументы | Основное содержимое `data` |
|---|---|---|
| `list_inforesources` | `filter`, `limit` | `resources`, `scope`, `complete`, `warnings` |
| `get_inforesource` | `path` | Имя, формат, количества вершин и дуг, происхождение |
| `get_subgraph` | `path`, `root=root`, `depth=2`, `max_nodes=2000` | `nodes`, `edges`, область и полнота |
| `count_graph` | `path`, `root?`, `depth?` | `vertices`, `edges`, `external_edge_count`, `scope` |
| `query_graph` | `path`, `field=name`, `value`, `limit`, `match_mode=auto` | `nodes`, `total_matches`, `query`, полнота |
| `validate_resource` | `payload` | Результат проверки нормализованной структуры и количества |
| `export_inforesource` | `path` | Сам документ `normalized-demo-v1` или `normalized-iacpaas-v1` |
| `get_gas_specification` | `path`, `concept_id` | Газ, стандарт, нормативные `facts`, полнота и происхождение; только IACPaaS |
| `update_concept` | `path`, `concept_id`, `mark?`, `comment?` | Изменённый концепт; только разрешённый mock |
| `import_inforesource` | `payload`, `clear_if_exists=false` | Путь и признак замены; только разрешённый mock |

Сервер mock публикует 9 инструментов, IACPaaS — 10. Изменяющие инструменты остаются в схеме IACPaaS для общего контракта, но их вызов всегда отклоняется. Аннотации инструментов не заменяют проверку доступа.

## Результаты и ошибки

Успех возвращается в `structuredContent`:

```json
{
  "ok": true,
  "data": {"resources": [], "complete": false, "scope": "configured_iacpaas_catalog"},
  "backend": "iacpaas",
  "complete": false,
  "warnings": []
}
```

Это сокращённый пример формы, а не фактический каталог. `backend` имеет значение `mock` или `iacpaas`; реальные графовые результаты содержат `mock=false` и `source`. Значения `complete`, `scope`, `warnings` описывают конкретную операцию. Каталог отражает настроенные пути и не доказывает доступность каждого ресурса до чтения.

Ошибка исполнения — `CallToolResult(isError=true)` с JSON-текстом без успешного `structuredContent`:

```json
{"code":"not_found","message":"Ресурс не найден","retryable":false}
```

Коды включают `invalid_data`, `limit_exceeded`, `not_found`, `permission_denied`, `authentication_error`, `timeout`, `platform_unavailable`, `platform_error`. Ошибки конфигурации и неизвестного backend могут остановить запуск до MCP-сессии. Ошибки реальной IACPaaS не включают mock и не означают отсутствия данных во всём фонде.

## Лимиты разных слоёв

| Слой | Ограничение |
|---|---|
| MCP-аргументы и успешный envelope | По 1 MiB сериализованного JSON по умолчанию; это не весь JSON-RPC frame |
| Публичный список/поиск | `limit=1..100` |
| Подграф | `depth=0..5`, `max_nodes=1..2000` |
| Путь/идентификатор | До 1024 символов |
| Текстовый фильтр/изменение | До 4096 символов |
| HTTP-экспорт из IACPaaS | По умолчанию 16 MiB; `IACPAAS_EXPORT_BYTES` от 1 KiB до 64 MiB |
| Mock-граф | До 2000 вершин и 10000 экземпляров дуг |
| Реальный нормализованный граф | До 50000 вершин, 200000 вхождений, глубина universal до 128 |
| Собственный агент | Общий бюджет до 12 MCP-вызовов на запрос, включая раунды исправления |

Лимит экспорта платформы отличается от лимита ответа инструмента: MCP может загрузить полный ресурс, но вернуть только небольшой подграф. Превышение бюджета чтения возвращает `limit_exceeded`. Лимиты проекта не являются заявленными пределами платформы IACPaaS.

В `auto` поиск по `id`/`metatype` точный, по остальным полям — подстрочный; `exact` и `contains` задают режим явно. `total_matches` относится к этому запросу, `complete=false` обозначает усечённую выдачу. Семантика обхода и дуг описана в [платформенном слое](platform.md).

## Запись в mock

Импорт принимает только `normalized-demo-v1`. Существующий путь заменяется лишь при явном `clear_if_exists=true`. Перед мутацией сервер резервирует место для подтверждения. Если полное успешное подтверждение не помещается, возвращается `ok=true`, `data={"applied":true,"result_omitted":true}`, `complete=false`: изменение уже выполнено. Если не помещается и ограниченное подтверждение, операция отклоняется до записи.

Изменения существуют только в памяти mock. Прямые изменения реальных ресурсов не поддерживаются. Подробности политики: [сервер](server.md); проверка контракта и границы приёмки: [verification.md](verification.md).
