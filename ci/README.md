# Проверки и CI

```sh
uv sync --frozen
uv run --frozen ruff check .
uv run --frozen pytest -q
```

`.github/workflows/test.yml` запускает `ruff` и `pytest` на Ubuntu и Windows (Python 3.12 и 3.13), а отдельное задание собирает Docker Compose, ждёт `/healthz` и останавливает контейнер. `github-actions.yml.example` содержит ту же конфигурацию как переносимый шаблон; для изменения workflow у учётной записи или токена должно быть право `workflow`.

Результаты локальных проверок и покрытие тестов описаны в [verification.md](../docs/verification.md).
