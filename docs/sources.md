# Источники и выбор стека

## Входные документы

- «02_Описание_учебной_задачи_IACPaaS_MCP_Agent.docx» — источник требований.
- «01_Источники_и_материалы_IACPaaS_MCP_Agent.docx» — подборка материалов.
- «03_Анализ_проекта_IACPaaS_MCP_и_план_команды_2.docx» — расширенный план на четверых, адаптированный к трём зонам ответственности.

Исходные DOCX не публикуются автоматически: в репозиторий включены собственные инструкции и матрица требований.

## Первичные технические источники

- MCP Python SDK, линия v1: https://github.com/modelcontextprotocol/python-sdk/tree/v1.x
- Документация v1: https://py.sdk.modelcontextprotocol.io/v1/
- Новая линия SDK и миграция: https://py.sdk.modelcontextprotocol.io/migration/
- MCP tools: https://modelcontextprotocol.io/specification/latest/server/tools
- MCP transports: https://modelcontextprotocol.io/specification/latest/basic/transports
- VS Code MCP: https://code.visualstudio.com/docs/copilot/customization/mcp-servers
- VS Code custom agents: https://code.visualstudio.com/docs/copilot/customization/custom-agents
- IACPaaS JSON: https://iacpaas.dvo.ru/docs/Json/Json.html
- IACPaaS API: https://iacpaas.dvo.ru/docs/api/current/annotated.html
- OpenAI tool calling: https://platform.openai.com/docs/guides/function-calling

Для проекта сознательно закреплён `mcp==1.30.0`, а не неконтролируемое обновление до v2. README официальной ветки v1 указывает на её поддержку и необходимость ограничения `<2`; v2 — другая линия с изменёнными импортами. Мы не смешиваем примеры API этих линий. При обновлении SDK сначала запускаются контрактные и транспортные тесты.

Прямые зависимости заданы в pyproject.toml, транзитивные закреплены uv.lock. Наличие документации платформы не заменяет тест настоящего API.
