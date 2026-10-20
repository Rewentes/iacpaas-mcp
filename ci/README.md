# Running checks locally

The project uses real MCP sessions in its tests; this is not a collection of
hard-coded API responses presented as an integration run.

```sh
uv sync --frozen
uv run --frozen ruff check .
uv run --frozen pytest -q
```

The file `github-actions.yml.example` contains the same checks for GitHub Actions.
It is intentionally a template: the current GitHub CLI authorization has no
`workflow` scope, and no remote CI execution is claimed. To enable it, grant the
appropriate GitHub permission and place it at `.github/workflows/test.yml`.

The final local run evidence is in `docs/verification.md`. A mock test does not
prove access to the actual IACPaaS platform, real LLM credentials or the VS Code UI.
