import json

import pytest
from mcp.shared.memory import create_connected_server_and_client_session

from iacpaas_mcp.server import create_server
from standalone_agent.runtime import redact


@pytest.fixture
def anyio_backend():
    return "asyncio"


def test_redact_env_secret_crossing_truncation_boundary(monkeypatch):
    secret = "review-synthetic-credential-0123456789"
    monkeypatch.setenv("REVIEW_SECRET", secret)
    prefix = "x" * 2030
    result = redact(prefix + secret + "tail" * 100)
    assert result == (prefix + "[REDACTED]" + "tail" * 100)[:2048]
    assert "review-synthetic" not in result
    assert len(result) <= 2048
    assert len(redact("[REDACTED]" * 1000)) <= 2048


@pytest.mark.anyio
async def test_committed_oversized_update_returns_bounded_success(monkeypatch, caplog):
    from iacpaas_mcp.domain import GraphService

    monkeypatch.setenv("IACPAAS_ALLOW_WRITES", "1")
    monkeypatch.setenv("IACPAAS_WRITABLE_PREFIX", "/demo/")
    service = GraphService()
    payload = {
        "format": "normalized-demo-v1",
        "mock": True,
        "path": "/demo/review",
        "name": "review",
        "nodes": [{"id": "root", "name": "root", "metatype": "test", "value": "x" * 1_046_000}],
        "edges": [],
    }
    assert len(json.dumps({"payload": payload}).encode()) < 1_048_576
    with caplog.at_level("INFO", logger="iacpaas_mcp.audit"):
        async with create_connected_server_and_client_session(create_server(service)) as session:
            imported = await session.call_tool("import_inforesource", {"payload": payload})
            assert not imported.isError
            small = await session.call_tool(
                "update_concept",
                {"path": payload["path"], "concept_id": "root", "comment": "small"},
            )
            assert not small.isError
            assert small.structuredContent["data"]["concept"]["comment"] == "small"
            result = await session.call_tool(
                "update_concept",
                {"path": payload["path"], "concept_id": "root", "comment": "c" * 4096},
            )
            assert not result.isError, result
            body = result.structuredContent
            assert body["ok"] is True
            assert body["data"]["applied"] is True
            assert body["data"]["result_omitted"] is True
            assert "concept" not in body["data"]
            assert body["complete"] is False
            assert any("volatile mock" in warning for warning in body["warnings"])
            assert any("omitted" in warning for warning in body["warnings"])
            assert len(json.dumps(body, ensure_ascii=False).encode()) <= 1_048_576
            oversized_read = await session.call_tool(
                "export_inforesource", {"path": payload["path"]}
            )
            assert oversized_read.isError
            assert json.loads(oversized_read.content[0].text)["code"] == "limit_exceeded"
        async with create_connected_server_and_client_session(
            create_server(service, max_result_bytes=2_097_152)
        ) as reader:
            readback = await reader.call_tool("export_inforesource", {"path": payload["path"]})
            assert not readback.isError
            node = readback.structuredContent["data"]["nodes"][0]
            assert node["comment"] == "c" * 4096
            assert node["value"] == payload["nodes"][0]["value"]
    audit = [record.message for record in caplog.records if record.name == "iacpaas_mcp.audit"]
    assert audit == [
        "mock_write operation=import_inforesource status=success",
        "mock_write operation=update_concept status=success",
        "mock_write operation=update_concept status=success",
    ]


@pytest.mark.anyio
@pytest.mark.parametrize("operation", ["update_concept", "import_inforesource"])
async def test_tiny_result_budget_denies_before_mutation(operation, monkeypatch, caplog):
    from iacpaas_mcp.domain import GraphService

    monkeypatch.setenv("IACPAAS_ALLOW_WRITES", "1")
    monkeypatch.setenv("IACPAAS_WRITABLE_PREFIX", "/demo/")
    service = GraphService()
    before = service.export_inforesource("/demo/university")
    if operation == "update_concept":
        arguments = {"path": "/demo/university", "concept_id": "root", "comment": "changed"}
    else:
        arguments = {"payload": {**before, "path": "/demo/review-tiny"}}
    with caplog.at_level("INFO", logger="iacpaas_mcp.audit"):
        async with create_connected_server_and_client_session(
            create_server(service, max_result_bytes=100)
        ) as session:
            result = await session.call_tool(operation, arguments)
            assert result.isError, result
            assert json.loads(result.content[0].text)["code"] == "limit_exceeded"
        async with create_connected_server_and_client_session(create_server(service)) as reader:
            readback = await reader.call_tool("export_inforesource", {"path": "/demo/university"})
            assert not readback.isError
            assert readback.structuredContent["data"] == before
            if operation == "import_inforesource":
                missing = await reader.call_tool("get_inforesource", {"path": "/demo/review-tiny"})
                assert missing.isError
                assert json.loads(missing.content[0].text)["code"] == "not_found"
    audit = [record.message for record in caplog.records if record.name == "iacpaas_mcp.audit"]
    assert audit == [f"mock_write operation={operation} status=denied_or_failed"]
