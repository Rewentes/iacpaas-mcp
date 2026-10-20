import pytest


@pytest.mark.parametrize(
    "depth,ids,edge_count",
    [
        (0, ["root"], 0),
        (1, ["root", "faculty"], 2),
        (2, ["root", "faculty", "course"], 4),
        (5, ["root", "faculty", "course", "student"], 6),
    ],
)
def test_bfs_cycles_duplicate_edges_external_and_hop_depth(depth, ids, edge_count):
    from iacpaas_mcp.domain import GraphService

    service = GraphService()
    result = service.get_subgraph("/demo/university", depth=depth)
    assert [node["id"] for node in result["nodes"]] == ids
    assert len(result["edges"]) == edge_count
    assert result["complete"] is True
    assert result["scope"] == "local_reachable_depth"
    limited = service.get_subgraph("/demo/university", depth=5, max_nodes=2)
    assert len(limited["nodes"]) == 2
    assert limited["complete"] is False
    assert limited["warnings"]
    counts = service.count_graph("/demo/university")
    assert (counts["vertices"], counts["edges"]) == (5, 6)
    assert (counts["node_count"], counts["edge_count"], counts["external_edge_count"]) == (5, 6, 1)
    reached = service.count_graph("/demo/university", root="root")
    assert (reached["node_count"], reached["edge_count"]) == (4, 6)


def test_normalized_schema_rejects_invalid_without_universal_parser():
    from copy import deepcopy

    from iacpaas_mcp.domain import DomainError, GraphService

    service = GraphService()
    from iacpaas_mcp.adapters import MockBackend

    payload = MockBackend().get("/demo/university")
    valid = service.validate_resource(payload)
    assert valid["valid"] is True
    assert valid["node_count"] == 5
    invalid = []
    for key, value in [("format", "universal"), ("mock", False), ("path", "/../oops")]:
        item = deepcopy(payload)
        item[key] = value
        invalid.append(item)
    item = deepcopy(payload)
    item["nodes"].append(deepcopy(item["nodes"][0]))
    invalid.append(item)
    item = deepcopy(payload)
    item["edges"][0]["source"] = "missing"
    invalid.append(item)
    item = deepcopy(payload)
    item["nodes"][0]["unknown"] = "not allowed"
    invalid.append(item)
    item = deepcopy(payload)
    item["nodes"][0]["value"] = float("nan")
    invalid.append(item)
    for item in invalid:
        with pytest.raises(DomainError) as error:
            service.validate_resource(item)
        assert error.value.code == "invalid_data"


def test_import_update_export_are_volatile_roundtrippable_and_explicit():
    from iacpaas_mcp.domain import DomainError, GraphService

    service = GraphService()
    resource = service.export_inforesource("/demo/university")
    with pytest.raises(DomainError) as error:
        service.import_inforesource(resource)
    assert error.value.code == "invalid_data"
    resource["path"] = "/demo/copied"
    assert service.import_inforesource(resource)["path"] == "/demo/copied"
    service.update_concept("/demo/copied", "course", mark="reviewed", comment="Проверено")
    changed = service.export_inforesource("/demo/copied")
    course = next(node for node in changed["nodes"] if node["id"] == "course")
    assert (course["mark"], course["comment"]) == ("reviewed", "Проверено")
    assert "mark" not in next(node for node in resource["nodes"] if node["id"] == "course")
    service.import_inforesource(resource, clear_if_exists=True)
    assert service.export_inforesource("/demo/copied") == resource
    assert service.export_inforesource("/demo/university") == GraphService().export_inforesource(
        "/demo/university"
    )
    with pytest.raises(DomainError, match="Вершина"):
        service.update_concept("/demo/copied", "missing", mark="x")
    with pytest.raises(DomainError) as error:
        GraphService(backend="iacpaas")
    assert error.value.code == "unsupported_backend"


def test_query_and_direct_service_bounds_are_enforced():
    from iacpaas_mcp.domain import DomainError, GraphService

    service = GraphService()
    assert service.query_graph("/demo/university", value="курс")["nodes"][0]["id"] == "course"
    assert (
        service.query_graph("/demo/university", field="metatype", value="person")["nodes"][0]["id"]
        == "student"
    )
    result = service.query_graph("/demo/university", limit=2)
    assert len(result["nodes"]) == 2
    assert result["complete"] is False
    calls = [
        lambda: service.get_subgraph("/demo/university", depth=-1),
        lambda: service.get_subgraph("/demo/university", depth=6),
        lambda: service.get_subgraph("/demo/university", max_nodes=2001),
        lambda: service.get_subgraph("/demo/university", max_nodes=0),
        lambda: service.get_subgraph("/demo/university", depth=True),
        lambda: service.count_graph("/demo/university", depth=6),
        lambda: service.list_inforesources(limit=0),
        lambda: service.query_graph("/demo/university", field="__dict__"),
        lambda: service.query_graph("/demo/university", limit=-1),
        lambda: service.import_inforesource(
            service.export_inforesource("/demo/university"), clear_if_exists="false"
        ),
    ]
    for call in calls:
        with pytest.raises(DomainError) as error:
            call()
        assert error.value.code in {"invalid_data", "limit_exceeded"}


def test_catalog_metadata_and_missing_resource():
    from iacpaas_mcp.domain import DomainError, GraphService

    service = GraphService()
    result = service.list_inforesources("университет")
    assert result["resources"][0]["path"] == "/demo/university"
    assert result["complete"] is False
    assert result["warnings"]
    metadata = service.get_inforesource("/demo/university")
    assert metadata["node_count"] == 5
    assert metadata["edge_count"] == 6
    assert metadata["mock"] is True
    with pytest.raises(DomainError) as error:
        service.get_inforesource("/missing")
    assert error.value.code == "not_found"
    assert error.value.retryable is False
    assert service.list_inforesources("no-match")["resources"] == []
