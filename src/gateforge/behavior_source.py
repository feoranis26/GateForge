from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from gateforge.behavior import (
    BehaviorBuilder,
    BehaviorError,
    BehaviorGraph,
    BehaviorNode,
    BehaviorOperation,
)
from gateforge.source import (
    CellIdentifier,
    CellSnapshot,
    ConstantBit,
    ConstantValue,
    DesignSnapshot,
    ModuleDependencyGraph,
    SnapshotBitRef,
    SourceBit,
)
from gateforge.material import (
    MaterialDesign,
    MaterialDesignDigest,
    MaterialModulePortRef,
    MaterialModuleValueRef,
    MaterialNetId,
)
from gateforge.target import PortDirection


@dataclass(frozen=True, slots=True)
class BehaviorBitBinding:
    source: SnapshotBitRef
    value: str
    offset: int


@dataclass(frozen=True, slots=True)
class BehaviorPortBinding:
    name: str
    direction: PortDirection
    value: str
    attributes: tuple[tuple[str, str], ...] = ()


@dataclass(frozen=True, slots=True)
class BehaviorCellBinding:
    source: CellIdentifier
    port: str
    value: str


@dataclass(frozen=True, slots=True)
class BehaviorCapture:
    graph: BehaviorGraph
    revision: int
    module: str
    bits: tuple[BehaviorBitBinding, ...]
    ports: tuple[BehaviorPortBinding, ...]
    cells: tuple[BehaviorCellBinding, ...]

    def __post_init__(self) -> None:
        nodes = {node.identifier: node for node in self.graph.nodes}
        if len({item.source for item in self.bits}) != len(self.bits):
            raise BehaviorError("Duplicate behavior source-bit binding")
        for binding in self.bits:
            if binding.source.revision != self.revision or binding.source.module != self.module:
                raise BehaviorError("Behavior source binding belongs to another snapshot")
            node = nodes.get(binding.value)
            if node is None or not 0 <= binding.offset < node.width:
                raise BehaviorError("Invalid behavior source-bit value")
        if len({item.name for item in self.ports}) != len(self.ports):
            raise BehaviorError("Duplicate behavior port binding")
        if any(item.value not in nodes for item in (*self.ports, *self.cells)):
            raise BehaviorError("Behavior binding references an unknown value")
        if any(item.source.module != self.module for item in self.cells):
            raise BehaviorError("Behavior cell binding belongs to another module")


class BehaviorLowerer(Protocol):
    def lower(self, snapshot: DesignSnapshot) -> BehaviorCapture: ...


@dataclass(frozen=True, slots=True)
class MaterialBehaviorBinding:
    net: MaterialNetId
    port: str
    direction: PortDirection
    value: str
    offsets: tuple[int, ...]


@dataclass(frozen=True, slots=True)
class MaterialBehaviorBoundary:
    material_digest: MaterialDesignDigest
    behavior_digest: str
    bindings: tuple[MaterialBehaviorBinding, ...]


def bind_material_behavior(
    material: MaterialDesign, capture: BehaviorCapture
) -> MaterialBehaviorBoundary:
    ports = {item.name: item for item in capture.ports}
    widths = {node.identifier: node.width for node in capture.graph.nodes}
    bindings: list[MaterialBehaviorBinding] = []
    for net in material.nets:
        for attachment in net.attachments:
            if not isinstance(attachment, (MaterialModulePortRef, MaterialModuleValueRef)):
                continue
            port = ports.get(attachment.port)
            if attachment.module != capture.module or port is None:
                raise BehaviorError("Material boundary has no matching source behavior port")
            if attachment.direction != port.direction:
                raise BehaviorError("Material boundary direction differs from source behavior")
            offsets = (
                (attachment.bit,) if isinstance(attachment, MaterialModulePortRef)
                else attachment.bits
            )
            if not offsets or any(offset < 0 or offset >= widths[port.value] for offset in offsets):
                raise BehaviorError("Material boundary bit lies outside its behavior value")
            bindings.append(MaterialBehaviorBinding(
                net.identifier, port.name, port.direction, port.value, offsets
            ))
    return MaterialBehaviorBoundary(
        material.get_digest(), capture.graph.get_digest(),
        tuple(sorted(bindings, key=lambda item: (item.net.value, item.port, item.offsets))),
    )


@dataclass(frozen=True, slots=True)
class YosysCombinationalBehaviorLowerer:
    observation_ports: tuple[tuple[str, str], ...] = ()

    def lower(self, snapshot: DesignSnapshot) -> BehaviorCapture:
        modules = tuple(
            module for module in snapshot.modules.values()
            if "blackbox" not in module.attributes
        )
        if len(modules) != 1:
            raise BehaviorError("Behavior capture currently requires one flattened module")
        module = modules[0]
        ModuleDependencyGraph.from_module(module)
        builder = BehaviorBuilder()
        bits: dict[SnapshotBitRef, tuple[str, int]] = {}
        ports: list[BehaviorPortBinding] = []
        cells: list[BehaviorCellBinding] = []
        observations: dict[str, str] = {}
        observer_types = dict(self.observation_ports)

        def assign(sources: tuple[SourceBit, ...], value: str) -> None:
            for offset, source in enumerate(sources):
                if not isinstance(source, SnapshotBitRef):
                    raise BehaviorError("Behavior drivers cannot target constant bits")
                if source in bits:
                    raise BehaviorError("Behavior source bit has multiple drivers")
                bits[source] = (value, offset)

        def vector(sources: tuple[SourceBit, ...]) -> str:
            if all(isinstance(source, ConstantBit) for source in sources):
                value = sum(
                    (1 << offset) for offset, source in enumerate(sources)
                    if isinstance(source, ConstantBit) and source.value == ConstantValue.ONE
                )
                return builder.constant(value, len(sources))
            locations = tuple(bits.get(source) if isinstance(source, SnapshotBitRef) else None for source in sources)
            if locations and locations[0] is not None:
                value, start = locations[0]
                if all(location == (value, start + offset) for offset, location in enumerate(locations)):
                    return builder.extract(value, start, len(sources))
            words = {location[0] for location in locations if location is not None}
            if len(words) == 1:
                value = next(iter(words))
                width = builder.width(value)
                if len(sources) > width and all(location == (value, offset) for offset, location in enumerate(locations[:width])) and all(isinstance(source, ConstantBit) and source.value == ConstantValue.ZERO for source in sources[width:]):
                    return builder.resize(value, len(sources))
                if builder.width(value) == len(sources) and all(location is None or location == (value, offset) for offset, location in enumerate(locations)):
                    full_mask = (1 << len(sources)) - 1
                    zeros = sum(1 << offset for offset, source in enumerate(sources) if isinstance(source, ConstantBit) and source.value == ConstantValue.ZERO)
                    ones = sum(1 << offset for offset, source in enumerate(sources) if isinstance(source, ConstantBit) and source.value == ConstantValue.ONE)
                    if zeros:
                        mask = builder.constant(full_mask ^ zeros, len(sources))
                        value = builder.intern(BehaviorNode(BehaviorOperation.BIT_AND, len(sources), tuple(sorted((value, mask)))))
                    if ones:
                        mask = builder.constant(ones, len(sources))
                        value = builder.intern(BehaviorNode(BehaviorOperation.BIT_OR, len(sources), tuple(sorted((value, mask)))))
                    return value
            operands = tuple(
                builder.constant(int(source.value.value), 1)
                if isinstance(source, ConstantBit)
                else builder.extract(bits[source][0], bits[source][1])
                for source in sources
            )
            return builder.concat(operands)

        for port in module.ports.values():
            if port.direction == PortDirection.INOUT:
                raise BehaviorError("Settled behavior does not support INOUT ports")
        for sources in (
            *(port.bits for port in module.ports.values()),
            *(port.bits for cell in module.cells.values() for port in cell.ports.values()),
        ):
            if not sources:
                raise BehaviorError("Zero-width behavior values are unsupported")
            if any(
                isinstance(source, ConstantBit)
                and source.value not in {ConstantValue.ZERO, ConstantValue.ONE}
                for source in sources
            ):
                raise BehaviorError("Settled two-state behavior cannot contain X/Z values")
        for name, port in sorted(module.ports.items()):
            if port.direction == PortDirection.INPUT:
                value = builder.input(name, len(port.bits))
                assign(port.bits, value)
                ports.append(BehaviorPortBinding(name, port.direction, value, port.attributes))

        pending = dict(module.cells)
        for name, cell in pending.items():
            cell_type = cell.identifier.expected_type
            if cell_type not in {"$add", "$and", "$or", "$xor", "$not", "$pos", "$logic_not", "$reduce_bool", "$logic_and", "$logic_or", "$eq", "$ne", "$lt", "$le", "$gt", "$ge", "$mux", "$_NOT_", "$_BUF_", *observer_types}:
                raise BehaviorError(
                    f"Unsupported combinational behavior cell {module.name}.{name} ({cell_type}); "
                    "stateful and unknown operations require explicit semantics"
                )
            if any(port.direction == PortDirection.INOUT for port in cell.ports.values()):
                raise BehaviorError("Settled behavior does not support INOUT cell ports")

        while pending:
            progressed = False
            for name, cell in sorted(tuple(pending.items())):
                input_sources = tuple(
                    source for port in cell.ports.values()
                    if port.direction == PortDirection.INPUT for source in port.bits
                )
                if any(isinstance(source, SnapshotBitRef) and source not in bits for source in input_sources):
                    continue
                cell_type = cell.identifier.expected_type
                if cell_type in observer_types:
                    port_name = observer_types[cell_type]
                    if set(cell.ports) != {port_name} or cell.ports[port_name].direction != PortDirection.INPUT:
                        raise BehaviorError("Observation cell must contain exactly its declared input port")
                    value = vector(cell.ports[port_name].bits)
                    observations[f"cell:{name}:{port_name}"] = value
                    cells.append(BehaviorCellBinding(cell.identifier, port_name, value))
                else:
                    expected = {"A", "B", "S", "Y"} if cell_type == "$mux" else {"A", "B", "Y"} if cell_type in {"$add", "$and", "$or", "$xor", "$logic_and", "$logic_or", "$eq", "$ne", "$lt", "$le", "$gt", "$ge"} else {"A", "Y"}
                    if set(cell.ports) != expected:
                        raise BehaviorError(f"Unexpected ports for {cell_type}")
                    if any(
                        port.direction != (PortDirection.OUTPUT if port_name == "Y" else PortDirection.INPUT)
                        for port_name, port in cell.ports.items()
                    ):
                        raise BehaviorError(f"Unexpected port directions for {cell_type}")
                    inputs = {port_name: vector(cell.ports[port_name].bits) for port_name in sorted(expected - {"Y"})}
                    for port_name, value in inputs.items():
                        cells.append(BehaviorCellBinding(cell.identifier, port_name, value))
                    width = len(cell.ports["Y"].bits)
                    if cell_type == "$mux":
                        if cell.parameter("WIDTH").as_unsigned_int() != width or any(builder.width(inputs[name]) != width for name in ("A", "B")) or builder.width(inputs["S"]) != 1:
                            raise BehaviorError("Mux widths disagree with cell parameters")
                        value = builder.intern(BehaviorNode(BehaviorOperation.SELECT, width, (inputs["S"], inputs["B"], inputs["A"])))
                    elif cell_type in {"$eq", "$ne", "$lt", "$le", "$gt", "$ge"}:
                        signed = self._parameters(cell, ("A", "B", "Y"))
                        operand_width = max(builder.width(inputs[name]) for name in ("A", "B"))
                        operation = BehaviorOperation(("signed-" if signed and cell_type not in {"$eq", "$ne"} else "") + cell_type[1:])
                        operands = tuple(builder.resize(inputs[name], operand_width, signed=signed) for name in ("A", "B"))
                        value = builder.resize(builder.intern(BehaviorNode(operation, 1, operands)), width)
                    elif cell_type in {"$logic_and", "$logic_or"}:
                        self._parameters(cell, ("A", "B", "Y"))
                        operation = BehaviorOperation.LOGIC_AND if cell_type == "$logic_and" else BehaviorOperation.LOGIC_OR
                        value = builder.resize(builder.intern(BehaviorNode(operation, 1, (inputs["A"], inputs["B"]))), width)
                    elif cell_type == "$add":
                        signed = self._parameters(cell, ("A", "B", "Y"))
                        value = builder.add(inputs["A"], inputs["B"], width, signed=signed)
                    elif cell_type in {"$and", "$or", "$xor", "$not"}:
                        signed = self._parameters(cell, tuple(sorted(expected)))
                        operation = {"$and": BehaviorOperation.BIT_AND, "$or": BehaviorOperation.BIT_OR, "$xor": BehaviorOperation.BIT_XOR, "$not": BehaviorOperation.BIT_NOT}[cell_type]
                        operands = tuple(sorted(builder.resize(operand, width, signed=signed) for operand in inputs.values()))
                        value = builder.intern(BehaviorNode(operation, width, operands))
                    elif cell_type == "$pos":
                        signed = self._parameters(cell, ("A", "Y"))
                        value = builder.resize(inputs["A"], width, signed=signed)
                    elif cell_type == "$reduce_bool":
                        self._parameters(cell, ("A", "Y"))
                        zero = builder.constant(0, builder.width(inputs["A"]))
                        value = builder.resize(builder.intern(BehaviorNode(BehaviorOperation.NE, 1, (inputs["A"], zero))), width)
                    elif cell_type == "$logic_not":
                        self._parameters(cell, ("A", "Y"))
                        value = builder.resize(builder.intern(BehaviorNode(
                            BehaviorOperation.LOGIC_NOT, 1, (inputs["A"],)
                        )), width)
                    else:
                        if builder.width(inputs["A"]) != 1 or width != 1:
                            raise BehaviorError("Leaf Boolean cells must be scalar")
                        value = inputs["A"] if cell_type == "$_BUF_" else builder.intern(
                            BehaviorNode(BehaviorOperation.LOGIC_NOT, 1, (inputs["A"],))
                        )
                    assign(cell.ports["Y"].bits, value)
                    cells.append(BehaviorCellBinding(cell.identifier, "Y", value))
                del pending[name]
                progressed = True
            if not progressed:
                raise BehaviorError("Behavior contains feedback or undriven cell inputs")

        for name, port in sorted(module.ports.items()):
            if port.direction != PortDirection.OUTPUT:
                continue
            if any(isinstance(source, SnapshotBitRef) and source not in bits for source in port.bits):
                raise BehaviorError(f"Undriven behavior output {name!r}")
            value = vector(port.bits)
            observations[f"port:{name}"] = value
            ports.append(BehaviorPortBinding(name, port.direction, value, port.attributes))
        return BehaviorCapture(
            builder.build(observations), snapshot.revision, module.name,
            tuple(BehaviorBitBinding(source, value, offset) for source, (value, offset) in sorted(bits.items())),
            tuple(sorted(ports, key=lambda item: item.name)),
            tuple(sorted(cells, key=lambda item: (item.source, item.port))),
        )

    @staticmethod
    def _parameters(cell: CellSnapshot, ports: tuple[str, ...]) -> bool:
        for name in ports:
            if cell.parameter(f"{name}_WIDTH").as_unsigned_int() != len(cell.ports[name].bits):
                raise BehaviorError(f"Behavior width disagrees with cell parameter {name}_WIDTH")
        signedness = tuple(
            cell.parameter(f"{name}_SIGNED").as_unsigned_int()
            for name in ports if name != "Y"
        )
        if any(value not in {0, 1} for value in signedness):
            raise BehaviorError("Cell signedness must be zero or one")
        return all(signedness)