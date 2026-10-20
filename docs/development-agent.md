# Разработка роли 3: вертикальный TDD и проверка

Владелец: `src/standalone_agent/*`, `tests/test_agent.py`, `tests/test_agent_cli.py`, два VS Code JSON, custom agent, эта документация и `docs/agent.md`. Чужие исходники/pyproject/lock не изменялись; commit/push не выполнялись.

## RED → GREEN (фактические выполнения)

Рабочий каталог — корень проекта. Для независимого запуска до завершения editable-install использован `PYTHONPATH=src` и `.venv/bin/python`.

1. **Первый вертикальный путь: discovery → список → связанный путь → metadata.**
   - RED: `.venv/bin/python -m pytest tests/test_agent.py -q` — `1 failed`, отсутствует `standalone_agent`.
   - Минимальный пакет и runtime; GREEN: `PYTHONPATH=src .venv/bin/python -m pytest tests/test_agent.py -q` — `1 passed`.
   - Тест не принимает захардкоженный `/demo/university`: fake MCP-session отдаёт `/actual/discovered`, второй вызов обязан использовать именно этот путь. Настоящий subprocess SDK дополнительно проверен позже.
2. **Budget в batch + deny-by-default + trace + clarification.**
   - RED той же командой — `2 failed, 1 passed`: нет `Trace`, missing-path ошибочно вызывает инструмент.
   - Реализованы бюджет отдельных попыток, явное подтверждение записей, безопасный trace и уточнение; GREEN — `3 passed`.
3. **OpenAI HTTP-контракт и CLI gate.**
   - RED: `PYTHONPATH=src .venv/bin/python -m pytest tests/test_agent_cli.py -q` — `2 failed`: отсутствуют `models` и `cli`.
   - GREEN после адаптера `httpx` и CLI: объединённо `5 passed`. MockTransport проверяет действительно сформированные JSON HTTP-запросы, tool schema и корреляцию `tool_call_id`; это **не реальный LLM-вызов**.
4. **Запрет повтора записи при ошибке.**
   - RED: `PYTHONPATH=src .venv/bin/python -m pytest tests/test_agent.py::test_failed_write_is_not_retried_even_if_model_requests_it -q` — ожидался один вызов, реально три.
   - Добавлено отслеживание сигнатур attempted write; GREEN — повтор заблокирован даже когда модель снова просит его, первый вызов возвращает timeout.
5. **Редактирование `key` и ограничение trace.**
   - RED: `PYTHONPATH=src .venv/bin/python -m pytest tests/test_agent.py::test_trace_redacts_key_and_bounds_file -q` — `PRIVATE` присутствовал в `args.key`.
   - Добавлена редакция key; GREEN — тест проходит, JSONL остаётся парсируемым и ограниченным.
6. **Регрессионные acceptance-проверки настоящих транспортов.**
   - Настоящий stdio SDK запускает `python -m iacpaas_mcp.server`: tools/list и два связанных tools/call.
   - Настоящий Streamable HTTP с bearer-токеном: subprocess server на свободном loopback-порту, tools/list → list_inforesources → count_graph. Процесс гарантированно завершается в finally.
   - Настоящая запись через MCP с явно имитируемыми TTY/вводом `yes` и включённой серверной политикой, затем read-back **в той же сессии**: комментарий root действительно изменён. Это автоматический тест gate, не ручной пользовательский сеанс.
   - Настоящий not_found на `/missing/resource` возвращается модели, следующий get_inforesource использует обнаруженный `/demo/university`.
   - Проверена согласованность JSON/имени сервера/custom-agent; UI headless не тестировался.

## Итоговые команды

```bash
PYTHONPATH=src .venv/bin/python -m pytest tests/test_agent.py tests/test_agent_cli.py -q
.venv/bin/python -m ruff check src/standalone_agent tests/test_agent.py tests/test_agent_cli.py
.venv/bin/python -m ruff format --check src/standalone_agent tests/test_agent.py tests/test_agent_cli.py
```

Результат: **12 passed**, `All checks passed!`, `7 files already formatted`, без warnings. В установленном SDK 1.30.0 проверены оба символа HTTP: legacy `streamablehttp_client` существует, но выдаёт deprecation warning; применён официальный `streamable_http_client(..., http_client=httpx.AsyncClient(...))`, реальный HTTP-тест зелёный.

Полный `PYTHONPATH=src .venv/bin/python -m pytest -q` в последнем прогоне дал **37 passed in 7.09s**. Ранее во время параллельной разработки был временный RED чужого серверного теста `test_write_warning_is_inside_result_byte_budget`; серверный workstream исправил его, роль 3 чужие файлы не меняла.

## Проверенный запуск CLI

```bash
PYTHONPATH=src .venv/bin/python -m standalone_agent 'найди университет и покажи метаданные' --model demo --trace traces/demo.jsonl --non-interactive
PYTHONPATH=src .venv/bin/python -m standalone_agent 'посчитай граф /demo/university' --model demo --trace traces/count.jsonl --non-interactive
PYTHONPATH=src .venv/bin/python -m standalone_agent 'покажи подграф /demo/university' --model demo --trace traces/subgraph.jsonl --non-interactive
PYTHONPATH=src .venv/bin/python -m standalone_agent 'покажи метаданные /missing/resource' --model demo --trace traces/recovery.jsonl --non-interactive
PYTHONPATH=src .venv/bin/python -m standalone_agent 'покажи подграф' --model demo --non-interactive
IACPAAS_ALLOW_WRITES=1 IACPAAS_WRITABLE_PREFIX=/demo/ PYTHONPATH=src .venv/bin/python -m standalone_agent 'измени комментарий /demo/university' --model demo --trace traces/write-denied.jsonl --non-interactive
```

Все exit 0; metadata: mock normalized-demo-v1, 5 узлов, 6 рёбер; count: vertices=5, edges=6, external_edge_count=1; missing-path: «Уточните путь ресурса»; noninteractive write: `permission_denied`, фактического mutating MCP call нет. Recovery trace содержит три вызова с промежуточным not_found.

HTTP CLI также фактически выполнен: сервер на `127.0.0.1:51689`, клиент `--transport http --url http://127.0.0.1:51689/mcp --model demo --trace traces/http.jsonl --non-interactive`, prompt `посчитай граф /demo/university`, exit 0, vertices=5/edges=6. Оба процесса очищены; токен — специально созданный синтетический тестовый, не пользовательский секрет.

`uv` отсутствовал в PATH этой shell, поэтому именно `uv run --frozen ...` здесь не был выполнен (команда вернула `uv: command not found`). Документированные uv-команды требуют установки uv оператором; все acceptance-проверки выше используют реальный доступный `.venv/bin/python`. Не заменяйте это утверждением о проверенном запуске через uv.

## Синтаксис VS Code и честные ограничения

Проверены официальные страницы `code.visualstudio.com/docs/agents/reference/mcp-configuration` (в том числе cwd, http headers, promptString/password) и `code.visualstudio.com/docs/copilot/customization/custom-chat-modes` (`.agent.md`, `tools: ['server/*']`). Новая документация продолжает поддерживать `.vscode/mcp.json` как compatibility-формат, но может рекомендовать portable `.mcp.json`; интерактивные inputs не универсальны для Agent Host. Поэтому HTTP-пример предназначен для локального VS Code extension-host, с отдельной ручной проверкой. Не заявлено ни проверки VS Code UI, ни реальной LLM, ни подключения к IACPaaS.
