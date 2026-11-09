"""Extract supported normative gas limits from the exported graph, never measurements."""

from .domain import DomainError

BOUNDS = {"Не более": "≤", "Не менее": "≥"}
UNITS = {"%", "ppm", "мг/м³"}


def gas_specification(resource, concept_id):
    nodes = {node["id"]: node for node in resource["nodes"]}
    gas = nodes.get(concept_id)
    if gas is None:
        raise DomainError("not_found", "Газ с указанным идентификатором не найден.")
    if gas["metatype"] != "Газ":
        raise DomainError(
            "invalid_data", "Выберите вершину понятия «Газ», найденную через query_graph."
        )
    outgoing = {}
    for edge in resource["edges"]:
        outgoing.setdefault(edge["source"], []).append(edge["target"])

    def children(identifier):
        return [nodes[target] for target in outgoing.get(identifier, []) if target in nodes]

    direct = children(concept_id)
    standards = [node.get("value") for node in direct if node["metatype"] == "Стандарт (норматив)"]
    standard = standards[0] if len(standards) == 1 else None
    containers = [node for node in direct if node["metatype"] == "Объемные доли компонентов"]
    facts, warnings = (
        [],
        ["Приведены нормативные ограничения, а не измеренное содержание примесей."],
    )
    complete = bool(containers)
    for container in containers:
        for component in children(container["id"]):
            if component["metatype"] != "Компонент":
                complete = False
                continue
            branches, seen, stack = [], set(), [(component, [])]
            while stack:
                node, ancestors = stack.pop()
                if node["id"] in seen:
                    continue
                seen.add(node["id"])
                branches.append((node, ancestors))
                stack.extend(
                    (child, [*ancestors, node]) for child in reversed(children(node["id"]))
                )
            symbols = list(
                dict.fromkeys(
                    str(node.get("value", node["name"]))
                    for node, _ in branches
                    if node["metatype"] == "Химическое обозначение"
                    and str(node.get("value", node["name"])) != "Химическое обозначение"
                )
            )
            if not symbols:
                symbols = [
                    node["name"]
                    for node in children(component["id"])
                    if node["metatype"] == "Водяные пары"
                ]
            label = " + ".join(symbols)
            if len(symbols) > 1:
                label += " (суммарно)"
            numeric = [
                (node, ancestors)
                for node, ancestors in branches
                if node["metatype"] == "Числовое значение"
                and type(node.get("value")) in (int, float)
            ]
            if not numeric:
                complete = False
            for node, ancestors in numeric:
                bound = next(
                    (parent for parent in reversed(ancestors) if parent["metatype"] in BOUNDS), None
                )
                units = set()
                for parent in reversed(ancestors):
                    units = {
                        child["value"]
                        for child in children(parent["id"])
                        if isinstance(child.get("value"), str) and child["value"] in UNITS
                    }
                    if units:
                        break
                operator = BOUNDS.get(bound["metatype"]) if bound else None
                explicit = (
                    {
                        child.get("value")
                        for child in children(bound["id"])
                        if isinstance(child.get("value"), str)
                    }
                    if bound
                    else set()
                )
                if not label or len(units) != 1 or operator not in explicit:
                    complete = False
                    continue
                facts.append(
                    {
                        "concept_id": node["id"],
                        "field": "нормативная объёмная доля",
                        "label": gas["name"] + " · " + label,
                        "value": node["value"],
                        "unit": next(iter(units)),
                        "operator": operator,
                        "value_kind": "normative_limit",
                    }
                )
    if not complete:
        warnings.append(
            "Часть структуры состава не поддерживается или отсутствует; полный состав не заявляется."
        )
    return {
        "path": resource["path"],
        "id": concept_id,
        "gas_name": gas["name"],
        "standard": standard,
        "facts": facts,
        "mock": resource["mock"],
        "complete": complete,
        "scope": "normative_gas_specification",
        "warnings": warnings,
        **({"source": resource["source"]} if "source" in resource else {}),
    }
