# Статус компонентов

| Компонент | Состояние |
|---|---|
| GraphService и MockBackend | реализованы и проверены |
| MCP tools, схемы, stdio и HTTP | реализованы и проверены |
| HTTP bearer auth и политика записи | реализованы и проверены |
| Python CLI и сценарный demo-режим | реализованы и проверены |
| OpenAI-совместимый профиль | реализован; тест HTTP-контракта, без live-прогона |
| Конфигурации и custom agent VS Code | подготовлены; ручная проверка GUI остаётся |
| Реальный IACPaaS backend | не подключён |
| GitHub Actions | шаблон, не активирован |

Порядок интеграции реальной платформы описан в [connect-iacpaas.md](connect-iacpaas.md). Результаты тестов и ограничения — в [verification.md](verification.md).
