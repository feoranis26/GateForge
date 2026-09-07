from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
import re
from types import MappingProxyType
from typing import Any

from gateforge.target import PortDirection


class SnapshotError(ValueError):
    pass


_BINARY_VALUE = re.compile(r"[01]+")


@dataclass(frozen=True, slots=True)
class YosysParameterValue:
    raw: str

    def as_unsigned_int(self) -> int:
        if _BINARY_VALUE.fullmatch(self.raw) is None:
            raise SnapshotError(
                f"Yosys parameter {self.raw!r} is not a known binary integer"
            )
        return int(self.raw, 2)

    def as_signed_int(self) -> int:
        unsigned = self.as_unsigned_int()
        sign_bit = 1 << (len(self.raw) - 1)
        return unsigned - (sign_bit << 1) if unsigned & sign_bit else unsigned

    def as_ascii_string(self) -> str:
        if not self.raw or any(ord(character) < 32 or ord(character) > 126 for character in self.raw):
            raise SnapshotError(
                f"Yosys parameter {self.raw!r} is not a printable ASCII string"
            )
        return self.raw

    def as_binary_bits(
        self,
        width: int,
        *,
        lsb_first: bool = True,
    ) -> tuple[int, ...]:
        if not isinstance(width, int) or isinstance(width, bool) or width <= 0:
            raise SnapshotError("Yosys parameter width must be positive")
        if _BINARY_VALUE.fullmatch(self.raw) is None or len(self.raw) != width:
            raise SnapshotError(
                f"Yosys parameter {self.raw!r} is not a {width}-bit known value"
            )
        bits = tuple(int(character) for character in self.raw)
        return tuple(reversed(bits)) if lsb_first else bits


class ConstantValue(StrEnum):
    ZERO = "0"
    ONE = "1"
    UNKNOWN = "x"
    HIGH_IMPEDANCE = "z"


@dataclass(frozen=True, slots=True, order=True)
class SnapshotBitRef:
    revision: int
    module: str
    bit_id: int


@dataclass(frozen=True, slots=True, order=True)
class ConstantBit:
    value: ConstantValue


type SourceBit = SnapshotBitRef | ConstantBit


@dataclass(frozen=True, slots=True, order=True)
class CellIdentifier:
    module: str
    name: str
    expected_type: str


@dataclass(frozen=True, slots=True, order=True)
class CellPortIdentifier:
    cell: CellIdentifier
    name: str
    bit: int


@dataclass(frozen=True, slots=True, order=True)
class ModulePortIdentifier:
    module: str
    name: str
    bit: int


type SourceEndpoint = CellPortIdentifier | ModulePortIdentifier


@dataclass(frozen=True, slots=True)
class ConstantBoundarySource:
    value: ConstantValue
    consumer: CellPortIdentifier


type BoundarySource = SnapshotBitRef | ConstantBoundarySource


@dataclass(frozen=True, slots=True)
class SourceCut:
    source: BoundarySource
    inside: frozenset[CellPortIdentifier]
    outside: frozenset[SourceEndpoint]


@dataclass(frozen=True, slots=True)
class CellPortSnapshot:
    name: str
    direction: PortDirection
    bits: tuple[SourceBit, ...]


@dataclass(frozen=True, slots=True)
class CellSnapshot:
    identifier: CellIdentifier
    ports: Mapping[str, CellPortSnapshot]
    parameters: Mapping[str, str]
    attributes: Mapping[str, str]

    @property
    def anchor(self) -> str | None:
        return self.attributes.get("gateforge_id")

    def parameter(self, name: str) -> YosysParameterValue:
        try:
            return YosysParameterValue(self.parameters[name])
        except KeyError as error:
            raise SnapshotError(
                f"Cell {self.identifier.module}.{self.identifier.name} has no "
                f"parameter {name!r}"
            ) from error


@dataclass(frozen=True, slots=True)
class ModulePortSnapshot:
    identifier: str
    direction: PortDirection
    bits: tuple[SourceBit, ...]


@dataclass(frozen=True, slots=True)
class ModuleSnapshot:
    revision: int
    name: str
    ports: Mapping[str, ModulePortSnapshot]
    cells: Mapping[str, CellSnapshot]
    attributes: Mapping[str, str]
    parameters: Mapping[str, str]
    endpoints: Mapping[SnapshotBitRef, frozenset[SourceEndpoint]]

    def derive_cut(self, claimed: frozenset[CellIdentifier]) -> frozenset[SourceCut]:
        if any(identifier.module != self.name for identifier in claimed):
            raise SnapshotError("A source-region cut must be module-local")

        claimed_names = {identifier.name for identifier in claimed}
        for identifier in claimed:
            cell = self.cells.get(identifier.name)
            if cell is None:
                raise SnapshotError(f"Cell {self.name}.{identifier.name} does not exist")
            if cell.identifier.expected_type != identifier.expected_type:
                raise SnapshotError(
                    f"Cell {self.name}.{identifier.name} has type "
                    f"{cell.identifier.expected_type!r}, expected "
                    f"{identifier.expected_type!r}"
                )

        cuts: list[SourceCut] = []
        handled_bits: set[SnapshotBitRef] = set()
        for identifier in sorted(claimed):
            cell = self.cells[identifier.name]
            for port in cell.ports.values():
                for index, source in enumerate(port.bits):
                    endpoint = CellPortIdentifier(identifier, port.name, index)
                    if isinstance(source, ConstantBit):
                        if port.direction in {PortDirection.INPUT, PortDirection.INOUT}:
                            cuts.append(
                                SourceCut(
                                    source=ConstantBoundarySource(source.value, endpoint),
                                    inside=frozenset({endpoint}),
                                    outside=frozenset(),
                                )
                            )
                        continue
                    if source in handled_bits:
                        continue
                    handled_bits.add(source)

                    endpoints = self.endpoints.get(source, frozenset())
                    inside = frozenset(
                        item
                        for item in endpoints
                        if isinstance(item, CellPortIdentifier)
                        and item.cell.name in claimed_names
                    )
                    outside = endpoints - inside
                    if outside:
                        cuts.append(
                            SourceCut(source=source, inside=inside, outside=outside)
                        )

        return frozenset(cuts)


@dataclass(frozen=True, slots=True)
class ModuleDependencyGraph:
    module: ModuleSnapshot
    drivers: Mapping[SnapshotBitRef, SourceEndpoint]
    consumers: Mapping[SnapshotBitRef, frozenset[SourceEndpoint]]

    @classmethod
    def from_module(cls, module: ModuleSnapshot) -> "ModuleDependencyGraph":
        drivers: dict[SnapshotBitRef, SourceEndpoint] = {}
        consumers: dict[SnapshotBitRef, frozenset[SourceEndpoint]] = {}
        for bit, endpoints in module.endpoints.items():
            bit_drivers: list[SourceEndpoint] = []
            bit_consumers: list[SourceEndpoint] = []
            for endpoint in endpoints:
                if isinstance(endpoint, CellPortIdentifier):
                    direction = module.cells[endpoint.cell.name].ports[
                        endpoint.name
                    ].direction
                    is_driver = direction == PortDirection.OUTPUT
                else:
                    direction = module.ports[endpoint.name].direction
                    is_driver = direction == PortDirection.INPUT
                if direction == PortDirection.INOUT:
                    raise SnapshotError(
                        f"Cannot index inout endpoint {endpoint} in module "
                        f"{module.name!r}"
                    )
                if is_driver:
                    bit_drivers.append(endpoint)
                else:
                    bit_consumers.append(endpoint)
            if len(bit_drivers) > 1:
                raise SnapshotError(
                    f"Signal {module.name}.{bit.bit_id} has multiple drivers: "
                    f"{sorted(bit_drivers)!r}"
                )
            if bit_drivers:
                drivers[bit] = bit_drivers[0]
            consumers[bit] = frozenset(bit_consumers)
        return cls(
            module=module,
            drivers=MappingProxyType(drivers),
            consumers=MappingProxyType(consumers),
        )

    def driver(self, bit: SnapshotBitRef) -> SourceEndpoint | None:
        return self.drivers.get(bit)

    def signal_consumers(
        self,
        bit: SnapshotBitRef,
    ) -> frozenset[SourceEndpoint]:
        return self.consumers.get(bit, frozenset())


@dataclass(frozen=True, slots=True)
class DesignSnapshot:
    revision: int
    modules: Mapping[str, ModuleSnapshot]

    @classmethod
    def from_json(cls, design: Mapping[str, Any], revision: int) -> "DesignSnapshot":
        modules = {
            name: _module_from_json(name, data, revision)
            for name, data in design.get("modules", {}).items()
        }
        return cls(revision=revision, modules=MappingProxyType(modules))

    def module(self, name: str) -> ModuleSnapshot:
        try:
            return self.modules[name]
        except KeyError as error:
            raise SnapshotError(f"Module {name!r} does not exist") from error


def _freeze_strings(values: Mapping[str, Any]) -> Mapping[str, str]:
    return MappingProxyType({str(key): str(value) for key, value in values.items()})


def _source_bit(value: object, revision: int, module: str) -> SourceBit:
    if isinstance(value, int) and not isinstance(value, bool):
        return SnapshotBitRef(revision, module, value)
    if isinstance(value, str):
        try:
            return ConstantBit(ConstantValue(value.lower()))
        except ValueError as error:
            raise SnapshotError(f"Unsupported Yosys constant bit {value!r}") from error
    raise SnapshotError(f"Unsupported Yosys bit value {value!r}")


def _direction(value: object, context: str) -> PortDirection:
    try:
        return PortDirection(str(value))
    except ValueError as error:
        raise SnapshotError(f"Unknown direction {value!r} for {context}") from error


def _module_from_json(
    name: str,
    data: Mapping[str, Any],
    revision: int,
) -> ModuleSnapshot:
    endpoint_index: dict[SnapshotBitRef, set[SourceEndpoint]] = {}
    ports: dict[str, ModulePortSnapshot] = {}

    for port_name, raw_port in data.get("ports", {}).items():
        bits = tuple(
            _source_bit(value, revision, name) for value in raw_port.get("bits", ())
        )
        port = ModulePortSnapshot(
            identifier=port_name,
            direction=_direction(raw_port.get("direction"), f"{name}.{port_name}"),
            bits=bits,
        )
        ports[port_name] = port
        for index, bit in enumerate(bits):
            if isinstance(bit, SnapshotBitRef):
                endpoint_index.setdefault(bit, set()).add(
                    ModulePortIdentifier(name, port_name, index)
                )

    cells: dict[str, CellSnapshot] = {}
    anchors: dict[str, str] = {}
    for cell_name, raw_cell in data.get("cells", {}).items():
        identifier = CellIdentifier(name, cell_name, str(raw_cell.get("type", "")))
        directions = raw_cell.get("port_directions", {})
        connections = raw_cell.get("connections", {})
        cell_ports: dict[str, CellPortSnapshot] = {}
        for port_name, raw_bits in connections.items():
            bits = tuple(_source_bit(value, revision, name) for value in raw_bits)
            port = CellPortSnapshot(
                name=port_name,
                direction=_direction(
                    directions.get(port_name), f"{name}.{cell_name}.{port_name}"
                ),
                bits=bits,
            )
            cell_ports[port_name] = port
            for index, bit in enumerate(bits):
                if isinstance(bit, SnapshotBitRef):
                    endpoint_index.setdefault(bit, set()).add(
                        CellPortIdentifier(identifier, port_name, index)
                    )

        attributes = _freeze_strings(raw_cell.get("attributes", {}))
        anchor = attributes.get("gateforge_id")
        if anchor:
            previous = anchors.get(anchor)
            if previous is not None:
                raise SnapshotError(
                    f"Duplicate gateforge_id {anchor!r} on sibling cells "
                    f"{name}.{previous} and {name}.{cell_name}"
                )
            anchors[anchor] = cell_name

        cells[cell_name] = CellSnapshot(
            identifier=identifier,
            ports=MappingProxyType(cell_ports),
            parameters=_freeze_strings(raw_cell.get("parameters", {})),
            attributes=attributes,
        )

    return ModuleSnapshot(
        revision=revision,
        name=name,
        ports=MappingProxyType(ports),
        cells=MappingProxyType(cells),
        attributes=_freeze_strings(data.get("attributes", {})),
        parameters=_freeze_strings(data.get("parameter_default_values", {})),
        endpoints=MappingProxyType(
            {bit: frozenset(endpoints) for bit, endpoints in endpoint_index.items()}
        ),
    )