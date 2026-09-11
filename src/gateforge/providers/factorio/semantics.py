from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING

from gateforge.providers.factorio.blueprint import build_finalized_factorio_blueprint
from gateforge.providers.factorio.realization import FactorioCircuitFabric, FactorioConstantRead, FactorioSignalRead
from gateforge.providers.factorio.routed import FactorioWireColor
from gateforge.realization import RealizationError, RealizedEndpoint

if TYPE_CHECKING:
    from gateforge.providers.factorio.finalization import FactorioFinalizedDesign


def _arithmetic(operation: str, left: int, right: int) -> int:
    if operation == "+":
        value = left + right
    elif operation == "*":
        value = left * right
    elif operation in {"=", "!=", "<", "<=", ">", ">="}:
        left = (left & 0xffffffff) - (1 << 32) if left & (1 << 31) else left
        right = (right & 0xffffffff) - (1 << 32) if right & (1 << 31) else right
        value = int({"=": left == right, "!=": left != right, "<": left < right, "<=": left <= right, ">": left > right, ">=": left >= right}[operation])
    elif operation == "AND":
        value = left & right
    elif operation == "OR":
        value = left | right
    elif operation == "XOR":
        value = left ^ right
    else:
        raise RealizationError("Unsupported arithmetic operation in simulation")
    return value & 0xffffffff


def simulate_combinational_fabric(
    fabric: FactorioCircuitFabric,
    inputs: Mapping[str, int],
    *,
    ticks: int | None = None,
) -> tuple[dict[str, int], ...]:
    bound = fabric.analyze().settling_ticks
    if ticks is None:
        ticks = bound + 2
    if isinstance(ticks, bool) or not isinstance(ticks, int) or ticks < 0:
        raise RealizationError("Simulation tick count must be a nonnegative integer")
    if set(inputs) != {item.name for item in fabric.inputs}:
        raise RealizationError("Simulation inputs must match external interface names")
    if any(isinstance(value, bool) or not isinstance(value, int) or not 0 <= value < (1 << 32) for value in inputs.values()):
        raise RealizationError("Simulation inputs must be unsigned 32-bit patterns")
    if any(inputs[item.name] >= (1 << item.width) for item in fabric.inputs):
        raise RealizationError("Simulation input exceeds declared width")
    endpoints = tuple(
        RealizedEndpoint(entity.identifier, port)
        for entity in fabric.design.entities for port in entity.ports
    )
    adjacency = {(endpoint, color): set() for endpoint in endpoints for color in FactorioWireColor}
    colors = {domain.identifier: domain.color for domain in fabric.domains}
    for wire in fabric.wires:
        source = (wire.source, colors[wire.domain])
        target = (wire.target, colors[wire.domain])
        adjacency[source].add(target)
        adjacency[target].add(source)
    networks: dict[tuple[RealizedEndpoint, FactorioWireColor], int] = {}
    for start in adjacency:
        if start in networks:
            continue
        network = len(networks)
        pending = [start]
        while pending:
            endpoint = pending.pop()
            if endpoint not in networks:
                networks[endpoint] = network
                pending.extend(adjacency[endpoint])

    def signed(value: int) -> int:
        value &= 0xffffffff
        return value - (1 << 32) if value >= (1 << 31) else value

    state = {item.entity: 0 for item in fabric.additions}
    history: list[dict[str, int]] = []
    for _ in range(ticks + 1):
        contents: dict[tuple[int, str], int] = {}

        def emit(endpoint: RealizedEndpoint, signal: str, value: int) -> None:
            for color in FactorioWireColor:
                key = (networks[endpoint, color], signal)
                contents[key] = signed(contents.get(key, 0) + value)

        for item in fabric.inputs:
            emit(item.endpoint, item.signal, signed(inputs[item.name]))
        for item in fabric.additions:
            emit(item.output, item.output_signal, state[item.entity])

        def read(selection: FactorioSignalRead | FactorioConstantRead) -> int:
            if isinstance(selection, FactorioConstantRead):
                return selection.value
            return signed(sum(
                contents.get((networks[selection.endpoint, color], selection.signal), 0)
                for color in selection.colors
            ))

        history.append({item.name: read(item.read) & 0xffffffff for item in fabric.observations})
        state = {item.entity: signed(_arithmetic(item.operation, read(item.first), read(item.second))) for item in fabric.additions}
    return tuple(history)


def simulate_finalized_combinational(
    finalized: FactorioFinalizedDesign,
    inputs: Mapping[str, int] | None = None,
    *,
    ticks: int | None = None,
) -> tuple[dict[str, int], ...]:
    blueprint = build_finalized_factorio_blueprint(finalized).canonical_data()["blueprint"]
    entities = {item["entity_number"]: item for item in blueprint["entities"]}
    numbers = {item.identifier: index for index, item in enumerate(finalized.entities, start=1)}
    inputs = dict(inputs) if inputs is not None else None
    if inputs is not None:
        if set(inputs) != {item.name for item in finalized.fabric.inputs}:
            raise RealizationError("Simulation inputs must match external interface names")
        if any(isinstance(value, bool) or not isinstance(value, int) or not 0 <= value < (1 << 32) for value in inputs.values()):
            raise RealizationError("Simulation inputs must be unsigned 32-bit patterns")
    elif any(entities[numbers[item.endpoint.entity]]["name"] != "constant-combinator" for item in finalized.fabric.inputs):
        raise RealizationError("External terminals require simulation stimuli")
    if inputs is not None and any(inputs[item.name] >= (1 << item.width) for item in finalized.fabric.inputs):
        raise RealizationError("Simulation input exceeds declared width")
    if ticks is None:
        ticks = finalized.settling_ticks + 2
    if isinstance(ticks, bool) or not isinstance(ticks, int) or ticks < 0:
        raise RealizationError("Simulation tick count must be a nonnegative integer")
    adjacency: dict[tuple[int, int], set[tuple[int, int]]] = {
        (number, connector): set() for number, entity in entities.items()
        for connector in (range(1, 5) if entity["name"] in {"arithmetic-combinator", "decider-combinator"} else range(1, 3))
    }
    for source_number, source_connector, target_number, target_connector in blueprint["wires"]:
        if source_connector == target_connector == 5:
            continue
        source = (source_number, source_connector)
        target = (target_number, target_connector)
        adjacency[source].add(target)
        adjacency[target].add(source)
    networks: dict[tuple[int, int], int] = {}
    for start in adjacency:
        if start in networks:
            continue
        identifier = len(networks)
        pending = [start]
        while pending:
            endpoint = pending.pop()
            if endpoint not in networks:
                networks[endpoint] = identifier
                pending.extend(adjacency[endpoint])
    additions = {
        number: entity["control_behavior"]["arithmetic_conditions"]
        for number, entity in entities.items() if entity["name"] == "arithmetic-combinator"
    }
    deciders = {number: entity["control_behavior"]["decider_conditions"] for number, entity in entities.items() if entity["name"] == "decider-combinator"}
    state = {number: 0 for number in (*additions, *deciders)}
    history: list[dict[str, int]] = []
    for _ in range(ticks + 1):
        contents: dict[tuple[int, str], int] = {}

        def emit(number, connectors, signal, value):
            for connector in connectors:
                key = (networks[number, connector], signal)
                contents[key] = (contents.get(key, 0) + value) & 0xffffffff

        for emission in finalized.fabric.inputs:
            number = numbers[emission.endpoint.entity]
            entity = entities[number]
            if entity["name"] == "constant-combinator":
                item = next(item for item in entity["control_behavior"]["sections"]["sections"][0]["filters"] if item["name"] == emission.signal)
                emit(number, (1, 2), item["name"], item["count"] if inputs is None else inputs[emission.name])
            else:
                assert inputs is not None
                emit(number, (1, 2), emission.signal, inputs[emission.name])
        for number, conditions in additions.items():
            emit(number, (3, 4), conditions["output_signal"]["name"], state[number])
        for number, conditions in deciders.items():
            emit(number, (3, 4), conditions["outputs"][0]["signal"]["name"], state[number])

        def read(number, signal, connectors):
            return sum(contents.get((networks[number, connector], signal), 0) for connector in connectors) & 0xffffffff

        output = {}
        for observation in finalized.fabric.observations:
            endpoint = observation.read.endpoint
            number = numbers[endpoint.entity]
            entity = entities[number]
            if entity["name"] == "small-lamp":
                signal = entity["control_behavior"]["circuit_condition"]["first_signal"]["name"]
                connectors = (1, 2)
            else:
                signal = observation.read.signal
                offset = 2 if entity["name"] in {"arithmetic-combinator", "decider-combinator"} and endpoint.port == "out" else 0
                connectors = tuple(offset + (1 if color == FactorioWireColor.RED else 2) for color in observation.read.colors)
            output[observation.name] = read(number, signal, connectors)
        history.append(output)
        following = {}
        for number, conditions in additions.items():
            operands = []
            for operand in ("first", "second"):
                if f"{operand}_constant" in conditions:
                    operands.append(conditions[f"{operand}_constant"])
                    continue
                selection = conditions[f"{operand}_signal_networks"]
                connectors = tuple(connector for color, connector in (("red", 1), ("green", 2)) if selection[color])
                operands.append(read(number, conditions[f"{operand}_signal"]["name"], connectors))
            following[number] = _arithmetic(conditions["operation"], *operands)
        for number, settings in deciders.items():
            condition = settings["conditions"][0]
            operands = []
            for operand in ("first", "second"):
                if f"{operand}_signal" not in condition:
                    operands.append(condition.get("constant", 0))
                else:
                    selection = condition[f"{operand}_signal_networks"]
                    connectors = tuple(connector for color, connector in (("red", 1), ("green", 2)) if selection[color])
                    operands.append(read(number, condition[f"{operand}_signal"]["name"], connectors))
            comparator = {"\u2260": "!=", "\u2264": "<=", "\u2265": ">="}.get(condition["comparator"], condition["comparator"])
            following[number] = settings["outputs"][0]["constant"] if _arithmetic(comparator, *operands) else 0
        state = following
    return tuple(history)


simulate_add_fabric = simulate_combinational_fabric
simulate_finalized_add = simulate_finalized_combinational