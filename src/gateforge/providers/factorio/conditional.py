from __future__ import annotations

from gateforge.behavior import BehaviorOperation
from gateforge.providers.factorio.realization import (
    COMPARATORS, FactorioArithmetic, FactorioCircuitDomain, FactorioCircuitFabric,
    FactorioConstantRead, FactorioDomainWire, FactorioInputEmission,
    FactorioObservationRead, FactorioSignalRead, _bind_interfaces, _interface_bindings,
)
from gateforge.providers.factorio.routed import FactorioWireColor
from gateforge.realization import RealizationDesign, RealizationError, RealizationProvenance, RealizedEndpoint, RealizedEntity, RealizedObservation
from gateforge.target import ProviderConfiguration


def propose_conditional_fabric(material, capture) -> FactorioCircuitFabric:
    graph = capture.graph
    nodes = {node.identifier: node for node in graph.nodes}
    values: dict[str, str | int] = {}
    entities = []
    inputs = []
    operations = []
    members = {}

    def connect(value, endpoint, color):
        if isinstance(value, int):
            value &= 0xffffffff
            return FactorioConstantRead(endpoint, value if value < (1 << 31) else value - (1 << 32))
        members.setdefault((value, color), set()).update((RealizedEndpoint(value, "out"), endpoint))
        return FactorioSignalRead(endpoint, "signal-A", (color,))

    def emit(identifier, operation, left, right):
        if operation in COMPARATORS and isinstance(left, int):
            if isinstance(right, int):
                left_signed = (left & 0xffffffff) - (1 << 32) if left & (1 << 31) else left
                right_signed = (right & 0xffffffff) - (1 << 32) if right & (1 << 31) else right
                return int({"=": left_signed == right_signed, "!=": left_signed != right_signed, "<": left_signed < right_signed, "<=": left_signed <= right_signed, ">": left_signed > right_signed, ">=": left_signed >= right_signed}[operation])
            left, right = right, left
            operation = {"=": "=", "!=": "!=", "<": ">", "<=": ">=", ">": "<", ">=": "<="}[operation]
        endpoint = RealizedEndpoint(identifier, "in")
        item = FactorioArithmetic(identifier, connect(left, endpoint, FactorioWireColor.RED), connect(right, endpoint, FactorioWireColor.GREEN), "signal-A", operation)
        operations.append(item)
        entities.append(RealizedEntity(identifier, item.prototype, ("in", "out")))
        return identifier

    def truth(identifier, value):
        return int(value != 0) if isinstance(value, int) else emit(identifier, "!=", value, 0)

    comparisons = {"eq": "=", "ne": "!=", "lt": "<", "le": "<=", "gt": ">", "ge": ">="}
    for node in graph.nodes:
        identifier = f"value:{node.identifier}"
        operands = [values[value] for value in node.operands]
        operation = node.operation
        if node.width not in {1, 32} and operation != BehaviorOperation.CONSTANT:
            raise RealizationError("Conditional realization supports Boolean and 32-bit values only")
        if operation == BehaviorOperation.INPUT:
            entities.append(RealizedEntity(identifier, "input-interface", ("out",)))
            inputs.append(FactorioInputEmission(node.name, RealizedEndpoint(identifier, "out"), "signal-A", node.width))
            value = identifier
        elif operation == BehaviorOperation.CONSTANT:
            if node.width > 32:
                raise RealizationError("Conditional constants cannot exceed 32 bits")
            value = node.value
        elif operation in {BehaviorOperation.ADD, BehaviorOperation.BIT_AND, BehaviorOperation.BIT_OR, BehaviorOperation.BIT_XOR, BehaviorOperation.BIT_NOT}:
            opcode = {BehaviorOperation.ADD: "+", BehaviorOperation.BIT_AND: "AND", BehaviorOperation.BIT_OR: "OR", BehaviorOperation.BIT_XOR: "XOR", BehaviorOperation.BIT_NOT: "XOR"}[operation]
            value = emit(identifier, opcode, operands[0], (1 << node.width) - 1 if operation == BehaviorOperation.BIT_NOT else operands[1])
            if node.width == 1 and operation == BehaviorOperation.ADD:
                value = emit(identifier + ":mask", "AND", value, 1)
        elif operation.value.removeprefix("signed-") in comparisons:
            left, right = operands
            comparator = comparisons[operation.value.removeprefix("signed-")]
            width = nodes[node.operands[0]].width
            if comparator not in {"=", "!="}:
                if width == 32 and not operation.value.startswith("signed-"):
                    left = (left ^ 0x80000000) if isinstance(left, int) else emit(identifier + ":bias-left", "XOR", left, 0x80000000)
                    right = (right ^ 0x80000000) if isinstance(right, int) else emit(identifier + ":bias-right", "XOR", right, 0x80000000)
                elif width == 1 and operation.value.startswith("signed-"):
                    left = -left if isinstance(left, int) else emit(identifier + ":sign-left", "*", left, -1)
                    right = -right if isinstance(right, int) else emit(identifier + ":sign-right", "*", right, -1)
            value = emit(identifier, comparator, left, right)
        elif operation == BehaviorOperation.LOGIC_NOT:
            value = emit(identifier, "=", operands[0], 0)
        elif operation in {BehaviorOperation.LOGIC_AND, BehaviorOperation.LOGIC_OR}:
            left, right = (truth(identifier + suffix, operand) for suffix, operand in zip((":truth-left", ":truth-right"), operands))
            value = emit(identifier, "AND" if operation == BehaviorOperation.LOGIC_AND else "OR", left, right)
        elif operation == BehaviorOperation.SELECT:
            condition, when_true, when_false = operands
            if isinstance(condition, int):
                value = when_true if condition else when_false
            elif when_true == when_false:
                value = when_true
            else:
                inverse = emit(identifier + ":inverse", "=", condition, 0)
                selected_true = emit(identifier + ":true", "*", condition, when_true)
                selected_false = emit(identifier + ":false", "*", inverse, when_false)
                value = emit(identifier, "+", selected_true, selected_false)
        elif operation == BehaviorOperation.ZERO_EXTEND:
            value = operands[0]
        elif operation == BehaviorOperation.SIGN_EXTEND and nodes[node.operands[0]].width == 1:
            value = -operands[0] if isinstance(operands[0], int) else emit(identifier, "*", operands[0], -1)
        elif operation in {BehaviorOperation.EXTRACT, BehaviorOperation.TRUNCATE} and node.width == 1 and node.value == 0:
            value = emit(identifier, "AND", operands[0], 1)
        else:
            raise RealizationError(f"Unsupported conditional value operation: {operation.value}")
        values[node.identifier] = value

    observations = []
    bindings = []
    native_lamps = {f"cell:{item.source.name}:{item.port}" for item in capture.cells if item.source.expected_type == "GF_Lamp" and item.port == "in"}
    for observation in graph.observations:
        endpoint = RealizedEndpoint(f"observe:{observation.name}", "circuit")
        value = values[observation.value]
        if isinstance(value, int):
            value = emit(f"constant:{observation.value}", "+", value, 0)
            values[observation.value] = value
        entities.append(RealizedEntity(endpoint.entity, "junction", ("circuit",), ProviderConfiguration.from_canonical_data({"native_lamp": True}) if observation.name in native_lamps else ProviderConfiguration()))
        observations.append(FactorioObservationRead(observation.name, connect(value, endpoint, FactorioWireColor.RED)))
        bindings.append(RealizedObservation(observation.name, observation.value, (endpoint,)))
    domains = tuple(FactorioCircuitDomain(f"{value}/{color.value}", color, tuple(sorted(endpoints))) for (value, color), endpoints in sorted(members.items()))
    wires = tuple(FactorioDomainWire(domain.identifier, domain.endpoints[0], endpoint) for domain in domains for endpoint in domain.endpoints[1:])
    design = RealizationDesign("factorio", material.get_digest(), graph.get_digest(), tuple(entities), tuple(bindings), (RealizationProvenance("factorio/conditional-cover/v1", tuple(item.identifier for item in entities), tuple(item.identifier for item in material.objects), tuple(nodes)),))
    fabric = _bind_interfaces(FactorioCircuitFabric(design, domains, wires, tuple(inputs), tuple(operations), tuple(observations)), _interface_bindings(capture))
    fabric.validate(material, graph)
    return fabric