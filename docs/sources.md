# Технические источники

- MCP Python SDK v1: https://github.com/modelcontextprotocol/python-sdk/tree/v1.x
- Документация SDK v1: https://py.sdk.modelcontextprotocol.io/v1/
- Миграция SDK: https://py.sdk.modelcontextprotocol.io/migration/
- MCP tools: https://modelcontextprotocol.io/specification/latest/server/tools
- MCP transports: https://modelcontextprotocol.io/specification/latest/basic/transports
- VS Code MCP: https://code.visualstudio.com/docs/copilot/customization/mcp-servers
- VS Code custom agents: https://code.visualstudio.com/docs/copilot/customization/custom-agents
- IACPaaS JSON: https://iacpaas.dvo.ru/docs/Json/Json.html
- IACPaaS API: https://iacpaas.dvo.ru/docs/api/current/annotated.html
- OpenAI tool calling: https://platform.openai.com/docs/guides/function-calling

## Версии

Используется `mcp==1.30.0` из поддерживаемой линии v1. При переходе на SDK v2 необходимы миграция импортов и повторная проверка MCP-контракта и транспортов.

Прямые зависимости закреплены в `pyproject.toml`, транзитивные — в `uv.lock`. Установка воспроизводится командой `uv sync --frozen`.
