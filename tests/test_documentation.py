"""Keep shipped examples and relative documentation links usable."""

import inspect
import json
import re
from pathlib import Path
from urllib.parse import unquote, urlsplit

import pytest

ROOT = Path(__file__).resolve().parents[1]


def test_shipped_json_examples_are_parseable():
    files = [*ROOT.glob("docs/**/*.json"), *ROOT.glob(".vscode/*.json")]
    assert files
    for path in files:
        json.loads(path.read_text(encoding="utf-8"))


def test_relative_documentation_links_exist():
    for path in [ROOT / "README.md", *ROOT.glob("docs/**/*.md")]:
        for link in re.findall(r"\[[^\]]*\]\(([^)]+)\)", path.read_text(encoding="utf-8")):
            url = urlsplit(link)
            if url.scheme or url.netloc or not url.path:
                continue
            assert (path.parent / unquote(url.path)).exists(), f"{path}: broken link {link}"


def test_documentation_index_covers_all_topic_pages():
    index = (ROOT / "docs/README.md").read_text(encoding="utf-8")
    targets = {urlsplit(link).path for link in re.findall(r"\[[^\]]*\]\(([^)]+)\)", index)}
    pages = {path.name for path in (ROOT / "docs").glob("*.md") if path.name != "README.md"}
    assert pages <= targets, f"Missing from documentation index: {pages - targets}"


@pytest.mark.asyncio
@pytest.mark.parametrize("backend,filename", [
    ("mock", "tool-schemas.json"), ("iacpaas", "tool-schemas-iacpaas.json"),
])
async def test_shipped_tool_schemas_match_current_server_without_platform_requests(
    backend, filename, monkeypatch,
):
    from importlib.metadata import version

    from iacpaas_mcp.domain import GraphService
    from iacpaas_mcp.platform import IACPaaSBackend
    from iacpaas_mcp.server import create_server

    def forbidden(*args, **kwargs):
        pytest.fail("Schema discovery must not request platform data")

    monkeypatch.setattr(IACPaaSBackend, "_request", forbidden)
    monkeypatch.setenv("IACPAAS_ALLOW_WRITES", "0")
    service = None if backend == "mock" else GraphService(IACPaaSBackend(
        username="synthetic-login", password="synthetic-password",
        resources={"fixture": "Fixture / Resource"},
    ))
    server = create_server(service, backend=backend)
    actual = [tool.model_dump(mode="json", exclude_none=True, exclude_unset=True)
              for tool in await server.list_tools()]
    saved = json.loads((ROOT / "docs" / filename).read_text(encoding="utf-8"))
    assert saved["sdk"] == "mcp==" + version("mcp")
    def normalize(value):
        if isinstance(value, list):
            return [normalize(item) for item in value]
        if isinstance(value, dict):
            return {key: inspect.cleandoc(item) if key == "description" and isinstance(item, str)
                    else normalize(item) for key, item in value.items()}
        return value

    assert normalize(saved["tools"]) == normalize(actual)
