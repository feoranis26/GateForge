from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from enum import StrEnum
from types import MappingProxyType

from gateforge.source import (
    CellSnapshot,
    DesignSnapshot,
    SnapshotError,
    YosysParameterValue,
)
from gateforge.target import PortDirection


INTRINSIC_ATTRIBUTE = "gateforge_intrinsic"
INTRINSIC_VERSION_ATTRIBUTE = "gateforge_intrinsic_version"


class IntrinsicError(ValueError):
    pass


class IntrinsicKind(StrEnum):
    TIMER = "timer"
    COUNTER = "counter"
    RANDOMIZER = "randomizer"
    SELECTOR = "selector"
    LAMP = "lamp"


@dataclass(frozen=True, slots=True)
class IntrinsicPort:
    name: str
    direction: PortDirection


@dataclass(frozen=True, slots=True)
class IntrinsicDefinition:
    module: str
    kind: IntrinsicKind
    version: int
    ports: tuple[IntrinsicPort, ...]


@dataclass(frozen=True, slots=True)
class IntrinsicInstance:
    definition: IntrinsicDefinition
    cell: CellSnapshot


class IntrinsicRegistry:
    def __init__(self, definitions: Iterable[IntrinsicDefinition]):
        definitions = tuple(definitions)
        by_module = {definition.module: definition for definition in definitions}
        if len(by_module) != len(definitions):
            raise IntrinsicError("Intrinsic registry contains duplicate module names")
        self._definitions = MappingProxyType(by_module)

    def recognize(
        self,
        design: DesignSnapshot,
        cell: CellSnapshot,
    ) -> IntrinsicInstance | None:
        module = design.modules.get(cell.identifier.expected_type)
        if module is None or INTRINSIC_ATTRIBUTE not in module.attributes:
            return None
        definition = self._definitions.get(cell.identifier.expected_type)
        if definition is None:
            raise IntrinsicError(
                f"Unsupported GateForge intrinsic module "
                f"{cell.identifier.expected_type!r}"
            )
        if module.attributes[INTRINSIC_ATTRIBUTE] != definition.kind.value:
            raise IntrinsicError(
                f"Intrinsic module {definition.module!r} has unexpected kind "
                f"{module.attributes[INTRINSIC_ATTRIBUTE]!r}"
            )
        try:
            version = YosysParameterValue(
                module.attributes[INTRINSIC_VERSION_ATTRIBUTE]
            ).as_unsigned_int()
        except (KeyError, SnapshotError) as error:
            raise IntrinsicError(
                f"Intrinsic module {definition.module!r} has no valid version"
            ) from error
        if version != definition.version:
            raise IntrinsicError(
                f"Intrinsic module {definition.module!r} has version {version}, "
                f"expected {definition.version}"
            )
        actual_ports = {
            name: port.direction for name, port in cell.ports.items()
        }
        expected_ports = {
            port.name: port.direction for port in definition.ports
        }
        if actual_ports != expected_ports:
            raise IntrinsicError(
                f"Intrinsic instance {cell.identifier.module}."
                f"{cell.identifier.name} has ports {actual_ports!r}, expected "
                f"{expected_ports!r}"
            )
        return IntrinsicInstance(definition, cell)


DEFAULT_INTRINSICS = IntrinsicRegistry(
    (
        IntrinsicDefinition(
            module="GF_Timer",
            kind=IntrinsicKind.TIMER,
            version=1,
            ports=(
                IntrinsicPort("in", PortDirection.INPUT),
                IntrinsicPort("reset", PortDirection.INPUT),
                IntrinsicPort("out", PortDirection.OUTPUT),
            ),
        ),
        IntrinsicDefinition(
            module="GF_Counter",
            kind=IntrinsicKind.COUNTER,
            version=1,
            ports=(
                IntrinsicPort("in", PortDirection.INPUT),
                IntrinsicPort("reset", PortDirection.INPUT),
                IntrinsicPort("out", PortDirection.OUTPUT),
            ),
        ),
        IntrinsicDefinition(
            module="GF_Randomizer",
            kind=IntrinsicKind.RANDOMIZER,
            version=1,
            ports=(
                IntrinsicPort("in", PortDirection.INPUT),
                IntrinsicPort("out", PortDirection.OUTPUT),
            ),
        ),
        IntrinsicDefinition(
            module="GF_Selector",
            kind=IntrinsicKind.SELECTOR,
            version=1,
            ports=(
                IntrinsicPort("cycle", PortDirection.INPUT),
                IntrinsicPort("in", PortDirection.INPUT),
                IntrinsicPort("out", PortDirection.OUTPUT),
            ),
        ),
        IntrinsicDefinition(
            module="GF_Lamp",
            kind=IntrinsicKind.LAMP,
            version=1,
            ports=(IntrinsicPort("in", PortDirection.INPUT),),
        ),
    )
)