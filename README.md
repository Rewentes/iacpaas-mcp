# IACPaaS MCP

MCP-сервер и агентские клиенты для работы с графовыми ресурсами IACPaaS. Сервер предоставляет общий набор инструментов для Python CLI и VS Code: поиск ресурсов, чтение метаданных, получение подграфов, подсчёт вершин и дуг, валидацию, импорт и экспорт.

## Возможности

- MCP через stdio и Streamable HTTP с общей доменной логикой.
- Типизированные аргументы, JSON Schema и структурированные результаты.
- Ограничения глубины, размера аргументов и ответа.
- Серверная политика доступа к изменяющим операциям и подтверждение записи в CLI.
- Python-агент с tool calling, обработкой ошибок, уточнением параметров и JSONL-трассой.
- Подключение OpenAI-совместимых моделей и конфигурация custom agent для VS Code.

В текущей версии используется `MockBackend` с синтетическим графом. Адаптер реального API IACPaaS находится в плане интеграции. Режим `demo` выполняет заданные сценарии без LLM и API-ключей; `openai` использует настроенную модель.

## Установка и быстрый запуск

Нужны Python 3.11+ и [uv](https://docs.astral.sh/uv/getting-started/installation/).

```sh
git clone https://github.com/Rewentes/iacpaas-mcp.git
cd iacpaas-mcp
uv sync --frozen
uv run --frozen iacpaas-agent "найди университет и покажи метаданные" --model demo --trace traces/demo.jsonl
```

При stdio-подключении агент запускает MCP-сервер автоматически.

## Архитектура

```text
Python CLI / VS Code
        │ MCP: stdio / Streamable HTTP
        ▼
MCP-сервер → GraphService → Backend
```

Клиенты используют один контракт инструментов. MCP-сервер проверяет аргументы и доступ, а GraphService выполняет операции над графом независимо от транспорта.

## Запуск сервера

```sh
# stdio
uv run --frozen iacpaas-mcp

# HTTP, после настройки MCP_HTTP_TOKEN в окружении
uv run --frozen iacpaas-mcp --transport http --host 127.0.0.1 --port 8000
```

Для подключения CLI к HTTP-серверу задайте тот же `MCP_HTTP_TOKEN`:

```sh
uv run --frozen iacpaas-agent "посчитай граф /demo/university" --model demo --transport http --url http://127.0.0.1:8000/mcp
```

HTTP без TLS поддерживается для loopback-подключений. Текущая конфигурация не предназначена для прямой публикации в интернет.

## Настройка модели

Скопируйте `.env.example` в `.env` и заполните `OPENAI_BASE_URL`, `OPENAI_API_KEY`, `OPENAI_MODEL`. Endpoint должен поддерживать Chat Completions и tool calling.

```sh
uv run --frozen --env-file .env iacpaas-agent "найди университет и покажи метаданные" --model openai
```

Приложения не загружают `.env` автоматически: используйте `--env-file` или экспорт переменных. Ключи и токены не должны попадать в git или трассы.

## VS Code

1. Откройте корень репозитория и выполните `uv sync --frozen`.
2. Запустите `iacpaas` через **MCP: List Servers**, используя `.vscode/mcp.json`.
3. Выберите custom agent **IACPaaS Mock Explorer** из `.github/agents/iacpaas.agent.md`.

Для HTTP предусмотрен `.vscode/mcp.http.example.json`. Инструкции и сценарии: [agent.md](docs/agent.md), [demo.md](docs/demo.md).

## Политика записи

Запись по умолчанию отключена. Для разрешения операций в `/demo/` оператор задаёт `IACPAAS_ALLOW_WRITES=1` и `IACPAAS_WRITABLE_PREFIX=/demo/`. CLI дополнительно запрашивает подтверждение пользователя; неинтерактивный режим отказывает в изменении.

MockBackend хранит изменения в памяти процесса. После перезапуска восстанавливаются исходные данные. Серверная политика действует независимо от настроек и инструкций агента.

## Проверки

```sh
uv run --frozen ruff check .
uv run --frozen pytest -q
```

Покрытие и результаты: [verification.md](docs/verification.md). Шаблон GitHub Actions: [ci/README.md](ci/README.md).

## Документация

- [Архитектура](docs/architecture.md).
- [Контракт MCP-инструментов](docs/contract.md) и [JSON Schema](docs/tool-schemas.json).
- [Графовые операции и backend](docs/platform.md).
- [MCP-сервер и политика доступа](docs/server.md).
- [Python-агент и VS Code](docs/agent.md).
- [Примеры использования](docs/demo.md).
- [Подключение API IACPaaS](docs/connect-iacpaas.md).
- [Статус интеграций](docs/status.md).
- [Технические источники](docs/sources.md).
