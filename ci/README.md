# Проверки и CI

```sh
uv sync --frozen
uv run --frozen ruff check .
uv run --frozen pytest -q
```

`github-actions.yml.example` содержит workflow для GitHub Actions. Для активации разместите его в `.github/workflows/test.yml`; у используемой учётной записи или токена должно быть право изменять workflow.

Результаты локальных проверок и покрытие тестов описаны в [verification.md](../docs/verification.md).
