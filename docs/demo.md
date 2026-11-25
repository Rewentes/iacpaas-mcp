# Демонстрация на синтетических данных

Все сценарии этой страницы используют `/demo/university`, а не газовую базу IACPaaS. `--model demo` — ограниченный сценарный парсер, не LLM. Он получает данные через настоящий MCP и не вызывает платного провайдера.

## Подготовка

Из корня проекта выполните `uv sync --frozen` и явно включите mock в каждом терминале стенда.

Bash:

```sh
export IACPAAS_BACKEND=mock
```

PowerShell:

```powershell
$env:IACPAAS_BACKEND = "mock"
```

Следующие команды одинаковы для обеих оболочек. Они не используют личную `.env`; поэтому обычный источник IACPaaS не подменяется неявно. Для реальных газов используйте [подключение IACPaaS](connect-iacpaas.md) и [клиент с моделью](agent.md).

## Чтение и восстановление после ошибки

```sh
uv run --frozen iacpaas-agent "найди университет и покажи метаданные" --model demo --non-interactive
uv run --frozen iacpaas-agent "посчитай граф /demo/university" --model demo --non-interactive
uv run --frozen iacpaas-agent "покажи подграф /demo/university" --model demo --non-interactive
uv run --frozen iacpaas-agent "покажи метаданные /missing/resource" --model demo --non-interactive
uv run --frozen iacpaas-agent "покажи подграф" --model demo --non-interactive
```

Первый сценарий вызывает каталог, затем метаданные пути из каталога. Полный mock-ресурс содержит 5 вершин и 6 экземпляров дуг; из корня достижимы 4 вершины. Подграф использует глубину 2. Неизвестный путь сначала действительно проверяется, затем сценарий выбирает известный путь из каталога. Последний запрос требует уточнения пути.

Для сохранения локальной трассы добавьте `--trace traces/demo.jsonl`. Схемы и эталоны: [контракт](contract.md), [семантика графа](platform.md). JSON-файлы в `docs/examples` относятся к этому синтетическому стенду и не являются отчётами реальной платформы.

## Отказ записи и явное подтверждение

При выключенной политике или `--non-interactive` изменение отклоняется:

```sh
uv run --frozen iacpaas-agent "измени комментарий /demo/university" --model demo --non-interactive
```

Для интерактивного опыта оператор отдельно задаёт `IACPAAS_ALLOW_WRITES=1` и `IACPAAS_WRITABLE_PREFIX=/demo/` в окружении терминала. Запустите ту же команду без `--non-interactive`, проверьте аргументы и подтвердите вводом `yes`. Меняется только память mock; новый stdio-процесс снова загружает исходную фикстуру. После опыта удалите флаг разрешения записи. Это не способ изменить реальные ресурсы.

## HTTP и два клиента

В первом терминале с `IACPAAS_BACKEND=mock` задайте собственный непустой `MCP_HTTP_TOKEN` безопасным способом, затем:

```sh
uv run --frozen iacpaas-mcp --backend mock --transport http --host 127.0.0.1 --port 8000
```

Во втором терминале задайте тот же токен:

```sh
uv run --frozen iacpaas-agent "посчитай граф /demo/university" --model demo --transport http --url http://127.0.0.1:8000/mcp --non-interactive
```

Подсчёт относится к одному и тому же серверному mock. Без правильного токена сервер возвращает 401. Пример схемы HTTP-подключения IDE находится в [.vscode/mcp.http.example.json](../.vscode/mcp.http.example.json).

## Проверка в IDE и завершение

Для mock-опыта в VS Code отдельно задайте `IACPAAS_BACKEND=mock` в личном окружении/конфигурации MCP и перезапустите сервер. Покажите вызовы каталога, метаданных, подсчёта и подграфа с `mock=true`. [Порядок подключения](agent.md).

После завершения остановите HTTP-сервер и уберите переменные mock, записи и тестового HTTP-токена из терминала. Следующая обычная сессия должна снова использовать реальную IACPaaS. Эти сценарии проверяют механику MCP, но не заменяют [предметную приёмку](acceptance.md).
