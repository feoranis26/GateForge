from __future__ import annotations

import z3

from gateforge.behavior import BehaviorGraph, BehaviorOperation
from gateforge.providers.factorio.realization import (
    FactorioChannelEquation,
    FactorioCircuitFabric,
    FactorioConstantRead,
    FactorioFabricAnalysis,
    COMPARATORS,
)
from gateforge.realization import RealizationError


def _fabric_expressions(fabric: FactorioCircuitFabric):
    inputs = {item.name: z3.BitVec(f"input:{item.name}", 32) for item in fabric.inputs}
    values = {(item.endpoint, item.signal): inputs[item.name] for item in fabric.inputs}
    depths = {channel: 0 for channel in values}

    def read(operand):
        if isinstance(operand, FactorioConstantRead):
            return z3.BitVecVal(operand.value, 32)
        return sum((values[endpoint, operand.signal] for endpoint in fabric.drivers(operand)), z3.BitVecVal(0, 32))

    pending = list(fabric.additions)
    while pending:
        following = []
        for operation in pending:
            drivers = tuple((endpoint, operand.signal) for operand in (operation.first, operation.second) for endpoint in fabric.drivers(operand))
            if any(channel not in values for channel in drivers):
                following.append(operation)
                continue
            left, right = read(operation.first), read(operation.second)
            if operation.operation == "+":
                value = left + right
            elif operation.operation == "*":
                value = left * right
            elif operation.operation in COMPARATORS:
                condition = {"=": left == right, "!=": left != right, "<": left < right, "<=": left <= right, ">": left > right, ">=": left >= right}[operation.operation]
                value = z3.If(condition, z3.BitVecVal(1, 32), z3.BitVecVal(0, 32))
            elif operation.operation == "AND":
                value = left & right
            elif operation.operation == "OR":
                value = left | right
            elif operation.operation == "XOR":
                value = left ^ right
            else:
                raise RealizationError("Unsupported arithmetic operation in equivalence proof")
            channel = (operation.output, operation.output_signal)
            values[channel] = z3.simplify(value)
            depths[channel] = 1 + max((depths[driver] for driver in drivers), default=0)
        if len(following) == len(pending):
            raise RealizationError("Factorio fabric has combinational feedback")
        pending = following
    observations = {item.name: read(item.read) for item in fabric.observations}
    bound = max((depths[endpoint, item.read.signal] for item in fabric.observations for endpoint in fabric.drivers(item.read)), default=0)
    return inputs, observations, bound


def analyze_bitvectors(fabric: FactorioCircuitFabric) -> FactorioFabricAnalysis:
    _inputs, observations, bound = _fabric_expressions(fabric)
    emitters = fabric.emitters()
    return FactorioFabricAnalysis(
        bound,
        tuple(FactorioChannelEquation(domain.identifier, signal, None)
              for domain in sorted(fabric.domains, key=lambda item: item.identifier)
              for signal in sorted({signal for endpoint, signal in emitters if endpoint in domain.endpoints})),
        tuple((name, None) for name in sorted(observations)),
    )


def _source_expressions(behavior: BehaviorGraph, inputs):
    values = {}
    for node in behavior.nodes:
        operands = tuple(values[identifier] for identifier in node.operands)
        operation = node.operation
        if operation == BehaviorOperation.INPUT:
            if node.width not in {1, 32}:
                raise RealizationError("Factorio interfaces require Boolean or 32-bit values")
            value = inputs[node.name] if node.width == 32 else z3.Extract(0, 0, inputs[node.name])
        elif operation == BehaviorOperation.CONSTANT:
            value = z3.BitVecVal(node.value, node.width)
        elif operation == BehaviorOperation.ADD:
            value = operands[0] + operands[1]
        elif operation == BehaviorOperation.BIT_AND:
            value = operands[0] & operands[1]
        elif operation == BehaviorOperation.BIT_OR:
            value = operands[0] | operands[1]
        elif operation == BehaviorOperation.BIT_XOR:
            value = operands[0] ^ operands[1]
        elif operation == BehaviorOperation.BIT_NOT:
            value = ~operands[0]
        elif operation == BehaviorOperation.EXTRACT:
            value = z3.Extract(node.value + node.width - 1, node.value, operands[0])
        elif operation == BehaviorOperation.TRUNCATE:
            value = z3.Extract(node.width - 1, 0, operands[0])
        elif operation == BehaviorOperation.ZERO_EXTEND:
            value = z3.ZeroExt(node.width - operands[0].size(), operands[0])
        elif operation == BehaviorOperation.SIGN_EXTEND:
            value = z3.SignExt(node.width - operands[0].size(), operands[0])
        elif operation == BehaviorOperation.CONCAT:
            value = z3.Concat(*reversed(operands))
        elif operation == BehaviorOperation.LOGIC_NOT:
            value = z3.If(operands[0] == 0, z3.BitVecVal(1, 1), z3.BitVecVal(0, 1))
        elif operation in {BehaviorOperation.LOGIC_AND, BehaviorOperation.LOGIC_OR}:
            condition = z3.And(operands[0] != 0, operands[1] != 0) if operation == BehaviorOperation.LOGIC_AND else z3.Or(operands[0] != 0, operands[1] != 0)
            value = z3.If(condition, z3.BitVecVal(1, 1), z3.BitVecVal(0, 1))
        elif operation == BehaviorOperation.SELECT:
            value = z3.If(operands[0] != 0, operands[1], operands[2])
        elif operation in {BehaviorOperation.EQ, BehaviorOperation.NE, BehaviorOperation.LT, BehaviorOperation.LE, BehaviorOperation.GT, BehaviorOperation.GE, BehaviorOperation.SIGNED_LT, BehaviorOperation.SIGNED_LE, BehaviorOperation.SIGNED_GT, BehaviorOperation.SIGNED_GE}:
            left, right = operands
            comparisons = {"eq": left == right, "ne": left != right}
            if operation.value.startswith("signed-"):
                comparisons.update({"lt": left < right, "le": left <= right, "gt": left > right, "ge": left >= right})
            else:
                comparisons.update({"lt": z3.ULT(left, right), "le": z3.ULE(left, right), "gt": z3.UGT(left, right), "ge": z3.UGE(left, right)})
            value = z3.If(comparisons[operation.value.removeprefix("signed-")], z3.BitVecVal(1, 1), z3.BitVecVal(0, 1))
        else:
            raise RealizationError("Unsupported source operation in equivalence proof")
        values[node.identifier] = value
    return {item.name: values[item.value] for item in behavior.observations}


def validate_bitvectors(fabric: FactorioCircuitFabric, behavior: BehaviorGraph) -> None:
    names = {node.name for node in behavior.nodes if node.operation == BehaviorOperation.INPUT}
    if {item.name for item in fabric.inputs} != names:
        raise RealizationError("Fabric external input identities differ from source")
    inputs, actual, _bound = _fabric_expressions(fabric)
    expected = _source_expressions(behavior, inputs)
    expected = {name: z3.ZeroExt(32 - value.size(), value) if value.size() < 32 else value for name, value in expected.items()}
    if actual.keys() != expected.keys() or any(actual[name].size() != expected[name].size() for name in expected):
        raise RealizationError("Fabric observation names or widths differ from source")
    solver = z3.Solver()
    solver.set(timeout=5000)
    widths = {node.name: node.width for node in behavior.nodes if node.operation == BehaviorOperation.INPUT}
    if any(item.width != widths[item.name] for item in fabric.inputs):
        raise RealizationError("Fabric external input widths differ from source")
    for name, width in widths.items():
        if width < 32:
            solver.add(z3.ULT(inputs[name], 1 << width))
    solver.add(z3.Or(*(actual[name] != expected[name] for name in expected)))
    result = solver.check()
    if result == z3.sat:
        model = solver.model()
        counterexample = {name: model.eval(value, model_completion=True).as_long() for name, value in inputs.items()}
        raise RealizationError(f"Fabric differs from source behavior; counterexample: {counterexample}")
    if result != z3.unsat:
        raise RealizationError(f"Could not prove fabric equivalence: {solver.reason_unknown()}")