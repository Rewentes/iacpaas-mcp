# Журнал разработки роли 1: platform/domain

Работа выполнена в `/home/hermes/projects/iacpaas-mcp`, Python из `.venv`.
Читался общий IMPLEMENTATION_AGREEMENT; изменения ограничены файлами роли 1.
Коммитов, push, реальных API-вызовов и изменений policy/server/agent не было.

## Реально выполненные вертикальные RED → GREEN

Команда полного набора роли:

```sh
PYTHONPATH=src .venv/bin/python -m pytest tests/test_backend.py tests/test_domain.py -q
```

| Вертикальный срез | Команда RED | Наблюдавшийся RED | Последующий GREEN |
|---|---|---|---|
| Mock catalog/get/save, независимые копии | `PYTHONPATH=src .venv/bin/python -m pytest tests/test_backend.py -q` | 1 failed: ModuleNotFoundError `iacpaas_mcp` — пакета еще не было | 1 passed после добавления пакета, fixture и адаптера |
| Каталог → метаданные → not_found | `PYTHONPATH=src .venv/bin/python -m pytest tests/test_domain.py -q` | 1 failed: ModuleNotFoundError `iacpaas_mcp.domain` | общий набор 2 passed |
| BFS с глубиной, циклом, повторными и внешними ребрами; подсчет | та же domain-команда | 4 failed, 1 passed: отсутствовал get_subgraph | общий набор 6 passed |
| Проверка собственного normalized-demo-v1 | та же domain-команда | 1 failed, 5 passed: отсутствовал validate_resource | общий набор 7 passed |
| Экспорт → импорт → обновление → повторное чтение; явный отказ реального backend | та же domain-команда | 1 failed, 6 passed: отсутствовал export_inforesource | общий набор 8 passed |
| Поиск и ограничения прямых вызовов сервиса | та же domain-команда | 1 failed, 7 passed: отсутствовал query_graph | общий набор 9 passed |
| Пользовательская fixture, отсутствие fallback при неверной схеме | backend-команда | 1 failed, 1 passed: DID NOT RAISE DomainError для формата universal | общий набор 10 passed после валидации при чтении |
| Уточнение межкомпонентного контракта: vertices/edges | domain-команда | 4 failed, 4 passed: KeyError vertices | затем финальный общий набор 11 passed |

Не писались сначала все тесты, а затем вся реализация: каждый перечисленный срез
добавлялся после зеленого предыдущего. Параметризация BFS проверяет один алгоритм
в нескольких глубинах. Первые два RED — отсутствие модулей, последующие — отсутствие
нужного поведения; это явно отмечено, а не выдается за assertion failure.
Последний добавленный тест `test_checked_in_reference_counts_match_actual_fixture`
— дополнительная **характеризационная проверка уже реализованного поведения**:
он прошел сразу, для него отдельный RED не заявляется и новый production-код не добавлялся.

## Рефакторинг и заключительные проверки

Первый запуск Ruff обнаружил два I001: порядок импортов адаптера и теста.
`ruff check --fix` исправил только эти два нарушения; `ruff format` отформатировал
только принадлежащие роли файлы. Повторные тесты зеленые.

Фактические финальные команды и вывод:

```text
PYTHONPATH=src .venv/bin/python -m pytest tests/test_backend.py tests/test_domain.py -q
........... [100%]
11 passed in 0.14s

.venv/bin/python -m ruff check src/iacpaas_mcp/domain.py src/iacpaas_mcp/adapters.py src/iacpaas_mcp/__init__.py tests/test_domain.py tests/test_backend.py
All checks passed!

.venv/bin/python -m ruff format --check src/iacpaas_mcp/domain.py src/iacpaas_mcp/adapters.py src/iacpaas_mcp/__init__.py tests/test_domain.py tests/test_backend.py
5 files already formatted
```

Дополнительный реальный Python smoke-вызов GraphService напечатал:
каталог `resources=[{path: /demo/university, name: Университет — синтетический mock}]`,
`complete=false`; весь ресурс `vertices=5, edges=6, external_edge_count=1`;
обход из root `vertices=4, edges=6, external_edge_count=1` с предупреждением
о незагружаемых внешних целях. Это реальные результаты выполнения mock, не платформы.

Проверены: изоляция экземпляров и копий, custom fixture, not_found, схема/неизвестные
поля/дубли id/неверные source/NaN/чужой формат, глубина 0/1/2/5, цикл,
повторные ребра, внешняя цель, max_nodes, поиск/лимиты, импорт с заменой и без,
изменение и повторный экспорт, unsupported_backend, файл эталонных количеств.

## Ограничения и что осталось интегратору

- Проверялся собственный набор роли 1, не весь параллельно создаваемый проект.
- Реального IACPaaS, production-аутентификации, сетевых запросов и конвертера
  universal/meta/simple нет. Скрытого fallback нет.
- Все изменения volatile; сериализация конкурентных записей, аудит, доверенная
  policy, подтверждение человека и предел размера ответа — внешние слои.
- `Backend` — порт для будущего адаптера; сейчас backend constructor принимает
  путь одного собственного normalized JSON-ресурса, не каталог произвольных экспортов.
- Упаковка `data/demo.json` в wheel — ответственность конфигурации родителя;
  этот срез проверен из src layout.
- Не каждый возможный ошибочный Python-вызов с несоответствующими аннотациям типами
  покрыт; внешние MCP аргументы дополнительно проверяет серверный слой.
