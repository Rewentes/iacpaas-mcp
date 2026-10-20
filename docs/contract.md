# Контракт инструментов

Формальный снимок, полученный настоящим `tools/list`, сохранён в [tool-schemas.json](tool-schemas.json). Он относится к `mcp==1.30.0` и текущему mock backend, а не к REST API платформы.

| Инструмент | Основные аргументы | Результат data |
|---|---|---|
| list_inforesources | filter, limit | resources, scope, complete, warnings |
| get_inforesource | path | path, name, format, node_count, edge_count |
| get_subgraph | path, root=root, depth=2, max_nodes=2000 | nodes, edges, root, depth, scope, complete |
| count_graph | path, root?, depth? | vertices, edges, external_edge_count, scope |
| query_graph | path, field=name, value, limit | nodes, total_matches, scope, complete |
| validate_resource | payload | valid, format, node_count, edge_count |
| export_inforesource | path | сам normalized-demo-v1 документ |
| update_concept | path, concept_id, mark?, comment? | concept и область volatile_mock_write |
| import_inforesource | payload, clear_if_exists=false | path, replaced и область volatile_mock_write |

Последние два инструмента изменяют только память mock-процесса. Для них необходима серверная политика; annotations не заменяют её. Вызов import для существующего пути без явного clear_if_exists не заменяет ресурс.

## Успешный результат

MCP `structuredContent` содержит envelope:

```json
{
  "ok": true,
  "data": {
    "path": "/demo/university",
    "vertices": 5,
    "edges": 6,
    "complete": true,
    "scope": "local_resource"
  },
  "backend": "mock",
  "complete": true,
  "warnings": []
}
```

Это сокращённый пример формы; полные фактические ответы есть в [examples/transport-smoke.json](examples/transport-smoke.json). Внутренние данные data различаются по операции. Выходная Pydantic-схема типизирует envelope; структура графа дополнительно проверяется доменной моделью.

## Ошибка

Ошибки исполнения приходят с MCP `isError=true`. Первый текстовый блок содержит JSON, например:

```json
{"code":"not_found","message":"Ресурс не найден: /missing/resource","retryable":false}
```

Основные коды: not_found, invalid_data, limit_exceeded, permission_denied, timeout, platform_error. unsupported_backend при выборе неподключённого IACPaaS возвращается как ошибка запуска до MCP-сессии. Не следует принимать пустую выдачу ограниченного каталога за отсутствие ресурса во всём фонде платформы.

## Текущие ограничения

- Глубина: 0..5; max_nodes: 1..2000.
- Публичный limit списка/поиска: 1..100 (доменные функции допускают больше, но MCP-контракт уже).
- Ограничение аргументов и сериализованного ответа: по 1 МиБ по умолчанию. Это не предел скачивания из платформы: внешнего HTTP-backend пока нет.
- JSON графа: до 2000 вершин и 10000 экземпляров дуг.
- Агент: до 12 попыток инструментов на запрос.
- Лимиты — настройки проекта, не ограничения IACPaaS.

При ограничении выдачи смотрите complete/warnings/scope. В count_graph без root/depth учитываются все локальные вершины, включая недостижимую от root. Внешняя цель не считается локальной вершиной; её дуга считается. Повторные дуги не сливаются.

## Предупреждение о записи

Перед изменением сервер проверяет, что ограниченное подтверждение результата помещается в бюджет. Обычная небольшая запись возвращает полный результат. Если после успешного изменения полный ответ слишком велик, сервер возвращает `ok=true`, `data={"applied":true,"result_omitted":true}`, `complete=false` и предупреждения; аудит отмечает success. Это неполная выдача результата, а не неуспешное изменение. Если даже ограниченное подтверждение не помещается, операция отклоняется до мутации. Для чтения превышение бюджета остаётся limit_exceeded. Любые сетевые таймауты будущего реального backend всё равно требуют проверки состояния без слепого повтора.
