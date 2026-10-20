# Разработка роли 2: фактические RED → GREEN

Интерпретатор: `.venv/bin/python` (Python 3.14). SDK: установленный официальный `mcp==1.30.0`; сигнатуры/исходники `FastMCP`, `Tool.run`, `FuncMetadata.convert_result`, `create_connected_server_and_client_session`, HTTP-клиента изучены через `inspect`, а не выдуманы. Исходники роли дополнительно прошли `ast.parse(..., feature_version=(3,11))`; полноценного исполнения на Python 3.11 здесь не было.

Каждый новый производственный срез начинался с запуска падающего теста. После GREEN добавлялись регрессионные проверки уже реализованных ветвей; их первый успешный запуск не выдаётся за новый RED.

| Вертикальный срез | Реальный RED | Реальный GREEN |
|---|---|---|
| Политика default deny, прямой вызов | `python -m pytest tests/test_policy.py -q`: assertion `find_spec('iacpaas_mcp.policy') is not None` — **1 failed** | После минимальной policy — **1 passed** |
| Startup opt-in и каноническая `/demo/` allowlist | Та же команда: разрешённый `/demo/university` ещё давал `PermissionError` — **1 failed, 1 passed** | После чтения env и проверки пути — **2 passed** |
| Первый end-to-end MCP tracer: каталог + outputSchema | `python -m pytest tests/test_server.py -q`: assertion отсутствующего server module — **1 failed** | После ToolResponse + FastMCP + list_inforesources, вместе с policy — **3 passed** |
| Поверхность чтения и JSON ошибок | `python -m pytest tests/test_server.py -q`: `Unknown tool: get_inforesource` — **1 failed, 1 passed** | Чтение и invalid depth/not_found прошли; следующий запуск с новым write-тестом показал **2 passed** и только write RED. Во время внедрения query_graph ещё не был готов в параллельном домене; после появления согласованного метода чтение стало GREEN без изменения чужих файлов |
| Запись: запрет direct call, startup capture, update/readback, audit | Отсутствующий write-handler давал `platform_error` вместо `permission_denied` — **1 failed, 2 passed** | После policy внутри обработчиков, RLock, write annotations и audit: server + policy — **5 passed** |
| Валидация и byte budgets | `...::test_validation_and_byte_budgets_precede_unbounded_work`: `create_server() got an unexpected keyword argument 'max_result_bytes'` — **1 failed** | После budget checks/validate tool: server + policy — **6 passed**. Первоначальное ожидание `{valid:false}` для пустого payload исправлено: домен по договорённому интерфейсу бросает DomainError; успех валидации проверяется на экспортированном валидном ресурсе |
| HTTP bearer gate, localhost, lifecycle | `python -m pytest tests/test_http.py -q`: отсутствовал `create_http_app` — **2 failed** | После ASGI gate и передачи lifespan — **2 passed** |
| CLI stdio и явный отказ native backend | `python -m pytest tests/test_server.py -k cli -q`: stdio `Connection closed`, native backend ошибочно завершался с 0 — **2 failed** | После main/argparse/default stdio: роль целиком — **10 passed** |
| Нельзя обойти auth публичным SDK HTTP factory | HTTP-тест: `DID NOT RAISE ValueError` при `create_server().streamable_http_app()` без токена | После override и вызова базовой FastMCP-фабрики внутри gate: роль целиком — **12 passed** |
| Предупреждение записи тоже входит в результатный budget | `...::test_write_warning_is_inside_result_byte_budget`: assertion `warning bypassed result byte limit` | Предупреждение добавлено до проверки размера; роль целиком — **13 passed** |

Уточнения по HTTP-проверкам:

- Дополнительный реальный TCP-тест создал CLI-подпроцесс, дождался HTTP 401, выполнил SDK initialize/tools-list/count_graph, завершил сессию и процесс.
- Uvicorn в установленной версии после штатного shutdown повторно поднимает SIGTERM: фактически returncode `-15`, но stderr содержит `Application shutdown complete`. Тест проверяет `(0, -15)` **и** сообщение о shutdown, а не приписывает процессу код 0.
- Первоначальные TestClient/deprecated SDK client выдавали предупреждения. HTTP-тесты переведены на `httpx.ASGITransport` + `session_manager.run()` и официальный `streamable_http_client(http_client=...)`. Финальный прогон роли не выдавал warnings. Настоящий subprocess-тест отдельно проверяет реальный ASGI lifespan CLI.

## Дополнительная регрессия после GREEN

- RuntimeError → санитизированный platform_error; TimeoutError → timeout/retryable=true, без внутреннего текста.
- MCP import → export readback на `/demo/imported`.
- Восемь direct calls из четырёх потоков: fake service вызван ровно восемь раз, максимум одновременно работающих мутаций — один. Это проверка локальной сериализации/отсутствия повторов, не сетевой тест платформы.

## Финальная проверка данного checkpoint

Из корня проекта:

```bash
.venv/bin/python -m ruff format src/iacpaas_mcp/server.py src/iacpaas_mcp/policy.py src/iacpaas_mcp/schemas.py tests/test_server.py tests/test_policy.py tests/test_http.py
.venv/bin/python -m ruff check src/iacpaas_mcp/server.py src/iacpaas_mcp/policy.py src/iacpaas_mcp/schemas.py tests/test_server.py tests/test_policy.py tests/test_http.py
.venv/bin/python -m pytest tests/test_server.py tests/test_policy.py tests/test_http.py -q
PYTHONPATH=src .venv/bin/python -m pytest -q
```

Фактический вывод последнего выполнения:

```text
1 file reformatted, 5 files left unchanged
All checks passed!
.................                                                        [100%]
17 passed in 3.13s
.........................................                                [100%]
41 passed in 7.03s
mcp 1.30.0
Python 3.11 grammar: OK
```

В более раннем полном прогоне без `PYTHONPATH` было **4 failed, 36 passed**: subprocess-тесты другой роли не находили пакет src-layout, так как сам проект ещё не был установлен в venv. Это не маскировалось: повтор с `PYTHONPATH=src` дал 40 passed; после дополнительного теста — 41 passed. `pytest.pythonpath` действует внутри pytest, но не устанавливает пакет для дочернего Python. Родитель интегрирует установку проекта (`uv sync --frozen`), lock и CI. В тестах этой роли subprocess явно получает `PYTHONPATH=src`.

## Границы доказательства

Тесты не обращались к реальному IACPaaS, API не подключён. Fake service используется только для независимого тестирования отказов/лимитов/сериализации; данные демонстрационных MCP-сессий берутся из настоящего локального GraphService/MockBackend. Никакого native universal/meta/simple parser не заявлено. HTTP-токены в тестах синтетические. Внешняя LLM и VS Code UI здесь не проверялись. Запись только volatile mock; post-write ошибка размера не обеспечивает rollback, автоматических повторов нет. `.env` сервер не загружает: используются только явно переданные переменные окружения.

Изменялись только файлы роли: `server.py`, `policy.py`, `schemas.py`, `tests/test_server.py`, `tests/test_policy.py`, `tests/test_http.py`, `docs/server.md`, `docs/development-server.md`. Коммитов и push не выполнялось.
