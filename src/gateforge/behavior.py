from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
import hashlib
import json


class BehaviorError(ValueError):
    pass


class BehaviorOperation(StrEnum):
    INPUT = "input"
    CONSTANT = "constant"
    ADD = "add"
    BIT_AND = "bit-and"
    BIT_OR = "bit-or"
    BIT_XOR = "bit-xor"
    BIT_NOT = "bit-not"
    EXTRACT = "extract"
    CONCAT = "concat"
    ZERO_EXTEND = "zero-extend"
    SIGN_EXTEND = "sign-extend"
    TRUNCATE = "truncate"
    LOGIC_NOT = "logic-not"
    LOGIC_AND = "logic-and"
    LOGIC_OR = "logic-or"
    EQ = "eq"
    NE = "ne"
    LT = "lt"
    LE = "le"
    GT = "gt"
    GE = "ge"
    SIGNED_LT = "signed-lt"
    SIGNED_LE = "signed-le"
    SIGNED_GT = "signed-gt"
    SIGNED_GE = "signed-ge"
    SELECT = "select"


class BehaviorTimingContract(StrEnum):
    SETTLED = "settled"


def _digest(data: object) -> str:
    return hashlib.sha256(json.dumps(
        data, sort_keys=True, separators=(",", ":"), allow_nan=False
    ).encode("utf-8")).hexdigest()


@dataclass(frozen=True, slots=True)
class BehaviorNode:
    operation: BehaviorOperation
    width: int
    operands: tuple[str, ...] = ()
    value: int = 0
    name: str = ""

    def __post_init__(self) -> None:
        if not isinstance(self.operation, BehaviorOperation):
            raise BehaviorError("Behavior operation must be a BehaviorOperation")
        if isinstance(self.width, bool) or not isinstance(self.width, int) or self.width <= 0:
            raise BehaviorError("Behavior width must be a positive integer")
        if not isinstance(self.operands, tuple):
            raise BehaviorError("Behavior operands must be an immutable tuple")
        if isinstance(self.value, bool) or not isinstance(self.value, int):
            raise BehaviorError("Behavior value must be an integer")
        if not isinstance(self.name, str):
            raise BehaviorError("Behavior name must be a string")
        if self.operation == BehaviorOperation.INPUT:
            if not self.name or self.operands or self.value:
                raise BehaviorError("Input requires a name and no operands or value")
        elif self.name:
            raise BehaviorError("Only input nodes may have names")
        if self.operation == BehaviorOperation.CONSTANT:
            if self.operands or not 0 <= self.value < (1 << self.width):
                raise BehaviorError("Constant must fit its width and have no operands")
        elif self.operation == BehaviorOperation.EXTRACT:
            if self.value < 0:
                raise BehaviorError("Extract offset must be nonnegative")
        elif self.value:
            raise BehaviorError("Only constants and extracts may have a value")

    def canonical_data(self) -> dict[str, object]:
        return {
            "operation": self.operation.value,
            "width": self.width,
            "operands": list(self.operands),
            "value": self.value,
            "name": self.name,
        }

    @property
    def identifier(self) -> str:
        return _digest(self.canonical_data())


def _validate_node(node: BehaviorNode, previous: Mapping[str, BehaviorNode]) -> None:
    try:
        operands = tuple(previous[identifier] for identifier in node.operands)
    except KeyError as error:
        raise BehaviorError("Behavior operand is missing or not topologically ordered") from error
    operation = node.operation
    if operation in {BehaviorOperation.INPUT, BehaviorOperation.CONSTANT}:
        return
    if operation in {BehaviorOperation.ADD, BehaviorOperation.BIT_AND, BehaviorOperation.BIT_OR, BehaviorOperation.BIT_XOR}:
        valid = len(operands) == 2 and all(item.width == node.width for item in operands)
    elif operation == BehaviorOperation.CONCAT:
        valid = bool(operands) and sum(item.width for item in operands) == node.width
    elif operation in {BehaviorOperation.EQ, BehaviorOperation.NE, BehaviorOperation.LT, BehaviorOperation.LE, BehaviorOperation.GT, BehaviorOperation.GE, BehaviorOperation.SIGNED_LT, BehaviorOperation.SIGNED_LE, BehaviorOperation.SIGNED_GT, BehaviorOperation.SIGNED_GE}:
        valid = len(operands) == 2 and operands[0].width == operands[1].width and node.width == 1
    elif operation in {BehaviorOperation.LOGIC_AND, BehaviorOperation.LOGIC_OR}:
        valid = len(operands) == 2 and node.width == 1
    elif operation == BehaviorOperation.SELECT:
        valid = len(operands) == 3 and operands[0].width == 1 and operands[1].width == operands[2].width == node.width
    elif len(operands) != 1:
        valid = False
    elif operation == BehaviorOperation.EXTRACT:
        valid = node.value + node.width <= operands[0].width
    elif operation in {BehaviorOperation.ZERO_EXTEND, BehaviorOperation.SIGN_EXTEND}:
        valid = node.width > operands[0].width
    elif operation == BehaviorOperation.TRUNCATE:
        valid = node.width < operands[0].width
    elif operation == BehaviorOperation.BIT_NOT:
        valid = node.width == operands[0].width
    else:
        valid = operation == BehaviorOperation.LOGIC_NOT and node.width == 1
    if not valid:
        raise BehaviorError(f"Invalid operands for {operation.value}")


@dataclass(frozen=True, slots=True)
class BehaviorObservation:
    name: str
    value: str


@dataclass(frozen=True, slots=True)
class BehaviorGraph:
    nodes: tuple[BehaviorNode, ...]
    observations: tuple[BehaviorObservation, ...]
    timing: BehaviorTimingContract = BehaviorTimingContract.SETTLED

    def __post_init__(self) -> None:
        if not isinstance(self.nodes, tuple) or not isinstance(self.observations, tuple):
            raise BehaviorError("Behavior graph collections must be immutable tuples")
        if self.timing is not BehaviorTimingContract.SETTLED:
            raise BehaviorError("Only settled combinational behavior is supported")
        previous: dict[str, BehaviorNode] = {}
        names: set[str] = set()
        for node in self.nodes:
            _validate_node(node, previous)
            if node.identifier in previous:
                raise BehaviorError("Duplicate behavior node")
            if node.operation == BehaviorOperation.INPUT:
                if node.name in names:
                    raise BehaviorError("Duplicate behavior input name")
                names.add(node.name)
            previous[node.identifier] = node
        observation_names: set[str] = set()
        for observation in self.observations:
            if not observation.name or observation.name in observation_names:
                raise BehaviorError("Behavior observations require unique nonempty names")
            if observation.value not in previous:
                raise BehaviorError("Observation references an unknown behavior value")
            observation_names.add(observation.name)

    def canonical_data(self) -> dict[str, object]:
        return {
            "schema_version": 1,
            "timing": self.timing.value,
            "nodes": [
                {"id": node.identifier, **node.canonical_data()}
                for node in sorted(self.nodes, key=lambda item: item.identifier)
            ],
            "observations": [
                {"name": item.name, "value": item.value}
                for item in sorted(self.observations, key=lambda item: item.name)
            ],
        }

    def get_digest(self) -> str:
        return _digest(self.canonical_data())

    def evaluate(self, inputs: Mapping[str, int]) -> dict[str, int]:
        required = {node.name for node in self.nodes if node.operation == BehaviorOperation.INPUT}
        if set(inputs) != required:
            raise BehaviorError("Input names do not match behavior graph")
        values: dict[str, int] = {}
        widths: dict[str, int] = {}
        for node in self.nodes:
            operands = tuple(values[identifier] for identifier in node.operands)
            operation = node.operation
            if operation == BehaviorOperation.INPUT:
                value = inputs[node.name]
                if isinstance(value, bool) or not isinstance(value, int):
                    raise BehaviorError("Behavior inputs must be integer bit patterns")
                if not 0 <= value < (1 << node.width):
                    raise BehaviorError(f"Input {node.name!r} does not fit its width")
            elif operation == BehaviorOperation.CONSTANT:
                value = node.value
            elif operation == BehaviorOperation.ADD:
                value = sum(operands)
            elif operation == BehaviorOperation.BIT_AND:
                value = operands[0] & operands[1]
            elif operation == BehaviorOperation.BIT_OR:
                value = operands[0] | operands[1]
            elif operation == BehaviorOperation.BIT_XOR:
                value = operands[0] ^ operands[1]
            elif operation == BehaviorOperation.BIT_NOT:
                value = ~operands[0]
            elif operation == BehaviorOperation.EXTRACT:
                value = operands[0] >> node.value
            elif operation == BehaviorOperation.CONCAT:
                value = 0
                offset = 0
                for identifier, operand in zip(node.operands, operands):
                    value |= operand << offset
                    offset += widths[identifier]
            elif operation == BehaviorOperation.SIGN_EXTEND:
                source_width = widths[node.operands[0]]
                value = operands[0]
                if value & (1 << (source_width - 1)):
                    value -= 1 << source_width
            elif operation == BehaviorOperation.LOGIC_NOT:
                value = int(operands[0] == 0)
            elif operation == BehaviorOperation.LOGIC_AND:
                value = int(operands[0] != 0 and operands[1] != 0)
            elif operation == BehaviorOperation.LOGIC_OR:
                value = int(operands[0] != 0 or operands[1] != 0)
            elif operation == BehaviorOperation.SELECT:
                value = operands[1] if operands[0] else operands[2]
            elif operation in {BehaviorOperation.EQ, BehaviorOperation.NE, BehaviorOperation.LT, BehaviorOperation.LE, BehaviorOperation.GT, BehaviorOperation.GE, BehaviorOperation.SIGNED_LT, BehaviorOperation.SIGNED_LE, BehaviorOperation.SIGNED_GT, BehaviorOperation.SIGNED_GE}:
                left, right = operands
                if operation.value.startswith("signed-"):
                    width = widths[node.operands[0]]
                    left = left - (1 << width) if left & (1 << (width - 1)) else left
                    right = right - (1 << width) if right & (1 << (width - 1)) else right
                comparator = operation.value.removeprefix("signed-")
                value = int({"eq": left == right, "ne": left != right, "lt": left < right, "le": left <= right, "gt": left > right, "ge": left >= right}[comparator])
            else:
                value = operands[0]
            values[node.identifier] = value & ((1 << node.width) - 1)
            widths[node.identifier] = node.width
        return {item.name: values[item.value] for item in self.observations}


class BehaviorBuilder:
    def __init__(self) -> None:
        self._nodes: dict[str, BehaviorNode] = {}

    def intern(self, node: BehaviorNode) -> str:
        _validate_node(node, self._nodes)
        self._nodes.setdefault(node.identifier, node)
        return node.identifier

    def width(self, identifier: str) -> int:
        return self._nodes[identifier].width

    def input(self, name: str, width: int) -> str:
        return self.intern(BehaviorNode(BehaviorOperation.INPUT, width, name=name))

    def constant(self, value: int, width: int) -> str:
        return self.intern(BehaviorNode(BehaviorOperation.CONSTANT, width, value=value))

    def extract(self, operand: str, offset: int, width: int = 1) -> str:
        if offset == 0 and width == self.width(operand):
            return operand
        return self.intern(BehaviorNode(BehaviorOperation.EXTRACT, width, (operand,), offset))

    def concat(self, operands: tuple[str, ...]) -> str:
        if len(operands) == 1:
            return operands[0]
        return self.intern(BehaviorNode(
            BehaviorOperation.CONCAT, sum(self.width(item) for item in operands), operands
        ))

    def resize(self, operand: str, width: int, *, signed: bool = False) -> str:
        if width == self.width(operand):
            return operand
        operation = (
            BehaviorOperation.TRUNCATE if width < self.width(operand)
            else BehaviorOperation.SIGN_EXTEND if signed else BehaviorOperation.ZERO_EXTEND
        )
        return self.intern(BehaviorNode(operation, width, (operand,)))

    def add(self, left: str, right: str, width: int, *, signed: bool = False) -> str:
        operands = tuple(sorted((
            self.resize(left, width, signed=signed),
            self.resize(right, width, signed=signed),
        )))
        return self.intern(BehaviorNode(BehaviorOperation.ADD, width, operands))

    def build(self, observations: Mapping[str, str]) -> BehaviorGraph:
        return BehaviorGraph(
            tuple(self._nodes.values()),
            tuple(BehaviorObservation(name, value) for name, value in sorted(observations.items())),
        )