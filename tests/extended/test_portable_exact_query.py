import pytest

from iacpaas_mcp.domain import DomainError, GraphService


def test_metatype_query_is_exact_and_count_scope_is_explicit():
    service = GraphService()
    resource = service.backend.get("/demo/university")
    for node, kind in zip(
        resource["nodes"], ["Газ", "Класс газов", "Онтология газов", "Газ", "Класс газовых смесей"]
    ):
        node["metatype"] = kind
    service.backend.save(resource)
    result = service.query_graph("/demo/university", field="metatype", value="газ", limit=1)
    assert result["total_matches"] == 2 and result["complete"] is False
    assert result["query"] == {"field": "metatype", "value": "газ", "match_mode": "exact"}
    assert (
        service.query_graph(
            "/demo/university", field="metatype", value="газ", match_mode="contains"
        )["total_matches"]
        == 5
    )
    with pytest.raises(DomainError):
        service.query_graph("/demo/university", match_mode="wrong")
