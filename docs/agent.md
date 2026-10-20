# Standalone MCP-агент и VS Code

## Режимы работы

`standalone_agent` реализует цикл «модель → инструменты → результат → модель» и подключается к серверу через MCP. `--model demo` — **ограниченный детерминированный сценарный парсер, не LLM**. Он распознаёт несколько русских/английских ключевых слов и получает все предметные данные через настоящий MCP; прямого импорта доменных сервисов нет. Backend — синтетический mock, не настоящая IACPaaS. Записи mock живут только в памяти отдельного сервера.

Установите зависимости из корня репозитория: `uv sync --frozen`. Далее:

```bash
uv run --frozen python -m standalone_agent "найди университет и покажи метаданные" --model demo --trace traces/demo.jsonl --non-interactive
uv run --frozen python -m standalone_agent "посчитай граф /demo/university" --model demo --trace traces/count.jsonl --non-interactive
uv run --frozen python -m standalone_agent "покажи подграф /demo/university" --model demo --trace traces/subgraph.jsonl --non-interactive
uv run --frozen python -m standalone_agent "покажи метаданные /missing/resource" --model demo --trace traces/recovery.jsonl --non-interactive
uv run --frozen python -m standalone_agent "покажи подграф" --model demo --non-interactive
```

Последняя команда возвращает просьбу уточнить путь; без `--non-interactive` в TTY агент задаёт вопрос и использует ответ в следующем ходе. Ошибочный явно заданный путь сначала действительно проверяется инструментом, затем `not_found` возвращается модели: demo выбирает путь из фактического каталога и делает ограниченную повторную попытку чтения. Для каждого стандартного успешного сценария есть `tools/list` и как минимум два реальных `tools/call`; JSONL фиксирует фактические аргументы/результаты, а не придуманные ответы.

## HTTP

В первом терминале (значение — самостоятельно выбранный локальный токен, не ключ LLM):

```bash
export MCP_HTTP_TOKEN='choose-a-local-demo-token'
uv run --frozen iacpaas-mcp --transport http --host 127.0.0.1 --port 8000
```

Во втором терминале экспортируйте **тот же** `MCP_HTTP_TOKEN`:

```bash
uv run --frozen python -m standalone_agent "найди университет и покажи метаданные" --transport http --url http://127.0.0.1:8000/mcp --model demo --trace traces/http.jsonl --non-interactive
```

Оба транспорта используют официальный MCP Python SDK 1.30.0: `ClientSession`, `stdio_client`, `streamable_http_client` (в 1.30.0 прежнее имя `streamablehttp_client` также существует, но уже deprecated). stdio запускает тот же Python (`sys.executable -m iacpaas_mcp.server`) из текущего каталога с окружением оператора. Не запускайте агент из недоверенного рабочего каталога. HTTP без TLS допускается только на loopback; токен передаётся заголовком Authorization, не инструменту.

## Запись: две независимые проверки

Из интерактивного терминала:

```bash
IACPAAS_ALLOW_WRITES=1 IACPAAS_WRITABLE_PREFIX=/demo/ uv run --frozen python -m standalone_agent "измени комментарий /demo/university" --model demo --trace traces/write.jsonl
```

Demo меняет комментарий `root` на «Демонстрационная запись mock». Агент показывает точные аргументы и требует буквальное `yes` от человека в TTY. Отсутствие TTY, EOF или `--non-interactive` означает **отказ**, даже при `confirmed=true` от модели. Нет флага autoapprove. Положительный ответ человека не отменяет серверную политику: нужны оба доверенных env-параметра, иначе сервер откажет. Ни агент, ни модель не могут включить серверную политику аргументами инструмента. Повторный вызов той же записи в рамках одного запуска блокируется, включая повтор после timeout; после неоднозначного сбоя сначала проверяйте состояние. В stdio новый процесс при каждом запуске означает новую копию mock-данных. В HTTP read-back делайте на том же сервере.

## Необязательная реальная модель

Только явный `--model openai` вызывает внешний API. Нужен совместимый с Chat Completions endpoint с поддержкой function/tool calling. Дополнительный SDK не требуется, используется `httpx`. Экспортируйте вручную:

```bash
export OPENAI_BASE_URL='https://api.openai.com/v1'
export OPENAI_MODEL='<your-tool-calling-model>'
# OPENAI_API_KEY задайте в окружении безопасно, не сохраняйте в git/истории команд.
uv run --frozen python -m standalone_agent "Найди университет и покажи метаданные" --model openai --trace traces/openai.jsonl --non-interactive
```

`OPENAI_BASE_URL` — базовый API URL, не полный `/chat/completions`. CLI **не загружает `.env` автоматически**: используйте экспорт переменных своей оболочкой. При отсутствии ключа/имени модели завершение с ошибкой происходит до соединения с сервером. Live-проверка внешнего LLM endpoint пока не выполнена. Контракт адаптера покрыт тестами с `httpx.MockTransport`. Данные инструментов будут отправлены выбранному провайдеру; используйте только допустимые данные.

## Ограничения и trace

Максимум 12 попыток вызова (`--max-calls 1..12`), в том числе каждый элемент batch отдельно, неизвестные инструменты и запрещённые записи. Число модельных ходов/уточнений тоже ограничено. MCP `isError` и JSON ошибки передаются обратно модели; `structuredContent` используется для успешных envelope. Неизвестные/непомеченные read-only инструменты требуют человеческого подтверждения (fail closed). Нет автоматических повторов записей или неограниченного восстановления.

Trace: JSONL `name,args,result,status,duration_ms`, mode `0600`, без следования symlink, родительские каталоги создаются. Файл **перезаписывается на каждый запуск**. Не более 256 KiB и примерно 16 KiB на событие; длинные списки/строки/результаты сокращаются. Ключи token/key/password/authorization и известные секретные env-значения редактируются, а также Bearer/sk-токены. Это best-effort redaction, а не DLP: произвольный неизвестный секрет в обычном тексте может остаться. Не подавайте секреты в инструменты и не публикуйте traces без проверки. Trace не является доверенным аудитом и намеренно не содержит полный модельный диалог. Результаты инструментов могут быть ограниченными: смотрите `complete`, `warnings`, область и лимиты.

## Подключение VS Code / Copilot

Нужны установленный `uv`, поддерживающая MCP версия VS Code, совместимый Copilot/LLM-доступ и разрешённые инструменты. Откройте корень проекта, выполните `uv sync --frozen`, просмотрите конфигурацию и примите Workspace Trust только для доверенного проекта. В `.vscode/mcp.json` сервер `iacpaas` запускается через `uv run --frozen iacpaas-mcp`, cwd `${workspaceFolder}`. Никаких абсолютных путей разработчика/токенов. Начальная конфигурация явно запрещает записи.

В Command Palette выберите **MCP: List Servers**, запустите `iacpaas`, просмотрите Output/список инструментов; подтвердите доверие серверу. В Chat выберите custom agent **IACPaaS Mock Explorer** (файл `.github/agents/iacpaas.agent.md`), разрешите его MCP-инструменты. Не включайте глобальное автоодобрение. Три read-сценария:

1. «Найди университет и покажи его метаданные». Ожидаются list_inforesources → get_inforesource с путём из каталога.
2. «Найди университет и посчитай вершины и экземпляры рёбер его графа». Ожидаются list_inforesources → count_graph.
3. «Найди университет и покажи подграф от root, глубина 2, максимум 100 вершин». Ожидаются list_inforesources → get_subgraph, вывод complete/warnings.

Для HTTP **замените**, а не подключайте второй раз одноимённый сервер: скопируйте содержимое `.vscode/mcp.http.example.json` в `.vscode/mcp.json`, запустите HTTP-сервер отдельно. Примерный файл автоматически VS Code не загружает. `inputs` запрашивает токен password-полем; не коммитьте введённое значение. `${input:...}` предназначен для локального VS Code extension-host: интерактивные inputs могут не пересылаться в Agent Host/Copilot harness. Используйте локальный чат для этого примера. По актуальной документации workspace `.vscode/mcp.json` поддерживается как compatibility-формат (некоторые новые интерфейсы предлагают portable `.mcp.json`).

Необязательная запись в VS Code: оператор вручную меняет stdio env на `IACPAAS_ALLOW_WRITES=1`, `IACPAAS_WRITABLE_PREFIX=/demo/`, перезапускает сервер, просит изменить комментарий root и отдельно подтверждает точные аргументы в диалоге/интерфейсе инструмента. После опыта верните запрет. Чат-инструкция не является жёстким CLI-gate: VS Code использует собственные подтверждения; обязательная серверная политика сохраняется. **UI не запускался в headless-среде: сценарии и trust/password prompts требуют ручной проверки.**

Официальные источники синтаксиса: https://code.visualstudio.com/docs/agents/reference/mcp-configuration (servers, cwd, inputs/password, http headers), https://code.visualstudio.com/docs/copilot/customization/custom-chat-modes (`.agent.md`, `tools: ['server/*']`), https://platform.openai.com/docs/guides/function-calling (Chat Completions tool calls). Контракты SDK дополнительно проверены по установленному `mcp==1.30.0`.
