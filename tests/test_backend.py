import pytest


def test_checked_in_reference_counts_match_actual_fixture():
    import json
    from pathlib import Path

    from iacpaas_mcp.domain import GraphService

    reference = json.loads(
        (Path(__file__).parents[1] / "fixtures/mock-reference-counts.json").read_text()
    )
    service = GraphService()
    for arguments, key in [({}, "whole_resource"), ({"root": "root"}, "reachable_from_root")]:
        actual = service.count_graph(reference["path"], **arguments)
        assert {field: actual[field] for field in reference[key]} == reference[key]
    for expected in reference["subgraphs"]:
        actual = service.count_graph(reference["path"], root="root", depth=expected["depth"])
        assert actual["vertices"] == expected["vertices"]
        assert actual["edges"] == expected["edges"]


def test_custom_fixture_is_used_and_invalid_schema_never_falls_back(tmp_path):
    import json

    from iacpaas_mcp.adapters import MockBackend
    from iacpaas_mcp.domain import DomainError, GraphService

    payload = MockBackend().get("/demo/university")
    payload["path"] = "/demo/custom"
    fixture = tmp_path / "custom.json"
    fixture.write_text(json.dumps(payload), encoding="utf-8")
    service = GraphService(MockBackend(fixture))
    assert service.get_inforesource("/demo/custom")["node_count"] == 5
    with pytest.raises(DomainError) as error:
        service.get_inforesource("/demo/university")
    assert error.value.code == "not_found"
    payload["format"] = "universal"
    fixture.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(DomainError) as error:
        GraphService(MockBackend(fixture)).get_inforesource("/demo/custom")
    assert error.value.code == "invalid_data"
    with pytest.raises(FileNotFoundError):
        MockBackend(tmp_path / "absent.json")


def test_mock_catalog_is_limited_and_copies_are_isolated():
    from iacpaas_mcp.adapters import MockBackend

    backend = MockBackend()
    catalog = backend.catalog()
    assert catalog[0]["path"] == "/demo/university"
    resource = backend.get("/demo/university")
    assert resource["format"] == "normalized-demo-v1"
    assert resource["mock"] is True
    resource["name"] = "changed"
    assert backend.get(resource["path"])["name"] != "changed"
    backend.save(resource)
    assert backend.get(resource["path"])["name"] == "changed"
    assert MockBackend().get(resource["path"])["name"] != "changed"
    with pytest.raises(KeyError):
        backend.get("/missing")
