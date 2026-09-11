from __future__ import annotations

from dataclasses import dataclass
import math
from typing import TYPE_CHECKING

from gateforge.target import ProviderConfiguration

if TYPE_CHECKING:
    from gateforge.providers.factorio.finalization import FactorioFinalizedDesign

from gateforge.providers.factorio.configuration import (
    FactorioLampConfiguration,
    FactorioObjectConfigurationCodec,
)
from gateforge.providers.factorio.routed import (
    FactorioEntity,
    FactorioRoutedDesign,
    FactorioSignalAssignment,
    FactorioWireColor,
)
from gateforge.providers.factorio.common import (
    FACTORIO_ARITHMETIC_COMBINATOR,
    FACTORIO_LAMP,
)


FACTORIO_BLUEPRINT_VERSION = 562949958467584


class FactorioBlueprintError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class FactorioBlueprintSignal:
    name: str
    type: str = "virtual"

    def __post_init__(self) -> None:
        if not self.name:
            raise FactorioBlueprintError("Factorio blueprint signal must have a name")
        if self.type not in {"item", "fluid", "recipe", "virtual"}:
            raise FactorioBlueprintError(
                f"Unsupported Factorio blueprint signal type {self.type!r}"
            )

    def canonical_data(self) -> dict[str, object]:
        return {"type": self.type, "name": self.name}


@dataclass(frozen=True, slots=True)
class FactorioConstantFilter:
    signal: FactorioBlueprintSignal
    count: int
    index: int = 1

    def __post_init__(self) -> None:
        if self.index <= 0:
            raise FactorioBlueprintError("Factorio constant filter index must be positive")
        if isinstance(self.count, bool) or not -(1 << 31) <= self.count < (1 << 31):
            raise FactorioBlueprintError("Factorio constant filter count must fit in int32")

    def canonical_data(self) -> dict[str, object]:
        return {
            "index": self.index,
            "type": self.signal.type,
            "name": self.signal.name,
            "quality": "normal",
            "comparator": "=",
            "count": self.count,
        }


@dataclass(frozen=True, slots=True)
class FactorioConstantControlBehavior:
    filters: tuple[FactorioConstantFilter, ...]

    def __post_init__(self) -> None:
        if not self.filters:
            raise FactorioBlueprintError(
                "Factorio constant combinator must have at least one filter"
            )
        indexes = tuple(item.index for item in self.filters)
        if len(set(indexes)) != len(indexes):
            raise FactorioBlueprintError(
                "Factorio constant combinator filter indexes must be unique"
            )

    def canonical_data(self) -> dict[str, object]:
        return {
            "sections": {
                "sections": [
                    {
                        "index": 1,
                        "filters": [
                            item.canonical_data()
                            for item in sorted(
                                self.filters,
                                key=lambda filter_item: filter_item.index,
                            )
                        ],
                    }
                ]
            }
        }


@dataclass(frozen=True, slots=True)
class FactorioNetworkSelection:
    red: bool
    green: bool

    @classmethod
    def from_color(cls, color: FactorioWireColor) -> "FactorioNetworkSelection":
        return cls(
            red=color == FactorioWireColor.RED,
            green=color == FactorioWireColor.GREEN,
        )

    def canonical_data(self) -> dict[str, object]:
        return {"red": self.red, "green": self.green}


@dataclass(frozen=True, slots=True)
class FactorioArithmeticControlBehavior:
    first_signal: FactorioBlueprintSignal | int
    second_signal: FactorioBlueprintSignal | int
    output_signal: FactorioBlueprintSignal
    first_networks: FactorioNetworkSelection
    second_networks: FactorioNetworkSelection
    operation: str = "+"

    def __post_init__(self) -> None:
        if self.operation not in {"+", "*", "AND", "OR", "XOR"}:
            raise FactorioBlueprintError("Unsupported Factorio arithmetic operation")
        for operand in (self.first_signal, self.second_signal):
            if not isinstance(operand, FactorioBlueprintSignal) and (isinstance(operand, bool) or not isinstance(operand, int) or not -(1 << 31) <= operand < (1 << 31)):
                raise FactorioBlueprintError("Arithmetic operand must be a signal or signed int32 constant")

    def canonical_data(self) -> dict[str, object]:
        conditions = {"operation": self.operation, "output_signal": self.output_signal.canonical_data()}
        for name, operand, networks in (("first", self.first_signal, self.first_networks), ("second", self.second_signal, self.second_networks)):
            if isinstance(operand, FactorioBlueprintSignal):
                conditions[f"{name}_signal"] = operand.canonical_data()
                conditions[f"{name}_signal_networks"] = networks.canonical_data()
            else:
                conditions[f"{name}_constant"] = operand
        return {"arithmetic_conditions": conditions}


@dataclass(frozen=True, slots=True)
class FactorioDeciderControlBehavior:
    first_signal: FactorioBlueprintSignal
    second: FactorioBlueprintSignal | int
    output_signal: FactorioBlueprintSignal
    first_networks: FactorioNetworkSelection
    second_networks: FactorioNetworkSelection
    comparator: str

    def __post_init__(self) -> None:
        if self.comparator not in {"=", "!=", "<", "<=", ">", ">="}:
            raise FactorioBlueprintError("Unsupported decider comparator")
        if not isinstance(self.first_signal, FactorioBlueprintSignal):
            raise FactorioBlueprintError("Decider first operand must be a signal")
        if not isinstance(self.second, FactorioBlueprintSignal) and (isinstance(self.second, bool) or not isinstance(self.second, int) or not -(1 << 31) <= self.second < (1 << 31)):
            raise FactorioBlueprintError("Decider second operand must be a signal or int32 constant")

    def canonical_data(self) -> dict[str, object]:
        condition = {"first_signal": self.first_signal.canonical_data(), "first_signal_networks": self.first_networks.canonical_data(), "comparator": {"!=": "\u2260", "<=": "\u2264", ">=": "\u2265"}.get(self.comparator, self.comparator), "compare_type": "and"}
        if isinstance(self.second, FactorioBlueprintSignal):
            condition["second_signal"] = self.second.canonical_data()
            condition["second_signal_networks"] = self.second_networks.canonical_data()
        else:
            condition["constant"] = self.second
        return {"decider_conditions": {"conditions": [condition], "outputs": [{"signal": self.output_signal.canonical_data(), "copy_count_from_input": False, "constant": 1}]}}


@dataclass(frozen=True, slots=True)
class FactorioLampControlBehavior:
    signal: FactorioBlueprintSignal
    comparator: str = ">"
    constant: int = 0

    def __post_init__(self) -> None:
        if self.comparator != ">" or self.constant != 0:
            raise FactorioBlueprintError(
                "Factorio lamps currently require a > 0 condition"
            )

    def canonical_data(self) -> dict[str, object]:
        return {
            "circuit_enabled": True,
            "circuit_condition": {
                "first_signal": self.signal.canonical_data(),
                "comparator": self.comparator,
                "constant": self.constant,
            },
        }


FactorioControlBehavior = (
    FactorioConstantControlBehavior
    | FactorioArithmeticControlBehavior
    | FactorioDeciderControlBehavior
    | FactorioLampControlBehavior
    | ProviderConfiguration
)


@dataclass(frozen=True, slots=True)
class FactorioBlueprintEntity:
    entity_number: int
    name: str
    x: float
    y: float
    direction: int | None = None
    control_behavior: FactorioControlBehavior | None = None

    def __post_init__(self) -> None:
        if self.entity_number <= 0:
            raise FactorioBlueprintError("Factorio entity number must be positive")
        if not self.name:
            raise FactorioBlueprintError("Factorio entity name must not be empty")
        if not math.isfinite(self.x) or not math.isfinite(self.y):
            raise FactorioBlueprintError("Factorio entity position must be finite")
        if self.direction is not None and self.direction not in range(16):
            raise FactorioBlueprintError("Factorio entity direction must be 0 through 15")

    def canonical_data(self) -> dict[str, object]:
        result: dict[str, object] = {
            "entity_number": self.entity_number,
            "name": self.name,
            "position": {"x": self.x, "y": self.y},
        }
        if self.direction is not None:
            result["direction"] = self.direction
        if self.control_behavior is not None:
            result["control_behavior"] = self.control_behavior.canonical_data()
        return result


@dataclass(frozen=True, slots=True, order=True)
class FactorioBlueprintWire:
    source_entity: int
    source_connector: int
    target_entity: int
    target_connector: int

    def __post_init__(self) -> None:
        if min(
            self.source_entity,
            self.source_connector,
            self.target_entity,
            self.target_connector,
        ) <= 0:
            raise FactorioBlueprintError(
                "Factorio blueprint wire values must be positive"
            )
        if (self.target_entity, self.target_connector) < (
            self.source_entity,
            self.source_connector,
        ):
            source_entity = self.source_entity
            source_connector = self.source_connector
            object.__setattr__(self, "source_entity", self.target_entity)
            object.__setattr__(self, "source_connector", self.target_connector)
            object.__setattr__(self, "target_entity", source_entity)
            object.__setattr__(self, "target_connector", source_connector)

    def canonical_data(self) -> list[int]:
        return [
            self.source_entity,
            self.source_connector,
            self.target_entity,
            self.target_connector,
        ]


@dataclass(frozen=True, slots=True)
class FactorioBlueprint:
    entities: tuple[FactorioBlueprintEntity, ...]
    wires: tuple[FactorioBlueprintWire, ...]
    label: str | None = None
    version: int = FACTORIO_BLUEPRINT_VERSION

    def __post_init__(self) -> None:
        if self.label is not None and not self.label:
            raise FactorioBlueprintError("Factorio blueprint label must not be empty")
        if self.version != FACTORIO_BLUEPRINT_VERSION:
            raise FactorioBlueprintError(
                f"Unsupported Factorio blueprint version {self.version}"
            )
        numbers = tuple(item.entity_number for item in self.entities)
        if numbers != tuple(range(1, len(numbers) + 1)):
            raise FactorioBlueprintError(
                "Factorio blueprint entity numbers must be contiguous from one"
            )
        number_set = set(numbers)
        for wire in self.wires:
            if (
                wire.source_entity not in number_set
                or wire.target_entity not in number_set
            ):
                raise FactorioBlueprintError(
                    "Factorio blueprint wire references an unknown entity"
                )
        if len(set(self.wires)) != len(self.wires):
            raise FactorioBlueprintError("Factorio blueprint wires must be unique")

    def canonical_data(self) -> dict[str, object]:
        blueprint: dict[str, object] = {
            "entities": [item.canonical_data() for item in self.entities],
            "wires": [item.canonical_data() for item in sorted(self.wires)],
            "item": "blueprint",
            "version": self.version,
        }
        if self.label is not None:
            blueprint["label"] = self.label
        return {"blueprint": blueprint}


def build_finalized_factorio_blueprint(
    finalized: FactorioFinalizedDesign,
    *,
    label: str | None = None,
) -> FactorioBlueprint:
    from gateforge.providers.factorio.finalization import physical_connector

    finalized.validate_physical()
    entities = finalized.entities
    entity_by_id = {item.identifier: item for item in entities}
    numbers = {item.identifier: index for index, item in enumerate(entities, start=1)}
    colors = {item.identifier: item.color for item in finalized.domains}

    def connector(endpoint, color):
        entity = entity_by_id[endpoint.entity]
        physical = physical_connector(endpoint, entity)
        return _connector_number(entity.prototype, physical.connector, color)

    return FactorioBlueprint(
        tuple(FactorioBlueprintEntity(
            numbers[item.identifier], item.prototype, item.x, item.y,
            _factorio_direction(item.angle) if item.prototype in {"arithmetic-combinator", "decider-combinator"} else None,
            None if item.configuration.is_empty else item.configuration,
        ) for item in entities),
        tuple(sorted((
            *(FactorioBlueprintWire(
                numbers[wire.source.entity], connector(wire.source, colors[wire.domain]),
                numbers[wire.target.entity], connector(wire.target, colors[wire.domain]),
            ) for wire in finalized.wires),
            *(FactorioBlueprintWire(numbers[segment.source], 5, numbers[segment.target], 5)
              for segment in finalized.power_segments),
        ))),
        label,
    )


def build_factorio_blueprint(
    routed: FactorioRoutedDesign,
    *,
    label: str | None = None,
) -> FactorioBlueprint:
    entity_numbers = {
        entity.identifier: index
        for index, entity in enumerate(routed.entities, start=1)
    }
    entities = tuple(
        _blueprint_entity(entity, entity_numbers[entity.identifier], routed)
        for entity in routed.entities
    )
    wires = tuple(
        sorted(
            {
            FactorioBlueprintWire(
                entity_numbers[wire.source.entity],
                _blueprint_connector(
                    routed,
                    wire.source.entity,
                    wire.source.connector,
                    wire.color,
                ),
                entity_numbers[wire.target.entity],
                _blueprint_connector(
                    routed,
                    wire.target.entity,
                    wire.target.connector,
                    wire.color,
                ),
            )
            for wire in routed.wires
            }
            | {
                FactorioBlueprintWire(
                    entity_numbers[segment.source],
                    5,
                    entity_numbers[segment.target],
                    5,
                )
                for segment in routed.power_segments
            }
        )
    )
    return FactorioBlueprint(entities, wires, label)


def _blueprint_entity(
    entity: FactorioEntity,
    entity_number: int,
    routed: FactorioRoutedDesign,
) -> FactorioBlueprintEntity:
    if entity.prototype == "constant-combinator":
        data = entity.configuration.canonical_data()
        if set(data) != {"signal", "count"}:
            raise FactorioBlueprintError(
                f"Constant combinator {entity.identifier!r} has unsupported configuration"
            )
        signal = data["signal"]
        count = data["count"]
        if not isinstance(signal, str):
            raise FactorioBlueprintError("Constant combinator signal must be a string")
        if isinstance(count, bool) or not isinstance(count, int):
            raise FactorioBlueprintError("Constant combinator count must be an integer")
        behavior: FactorioControlBehavior | None = FactorioConstantControlBehavior(
            (FactorioConstantFilter(FactorioBlueprintSignal(signal), count),)
        )
        direction = None
    elif entity.prototype == "arithmetic-combinator":
        configuration = FactorioObjectConfigurationCodec().decode(
            FACTORIO_ARITHMETIC_COMBINATOR,
            entity.configuration,
        )
        if not hasattr(configuration, "operation"):
            raise FactorioBlueprintError(
                f"Arithmetic combinator {entity.identifier!r} has invalid configuration"
            )
        if configuration.operation != "add":
            raise FactorioBlueprintError(
                f"Arithmetic combinator {entity.identifier!r} is not an add"
            )
        first = _require_signal(routed, entity.identifier, 1, "signal-A")
        second = _require_signal(routed, entity.identifier, 1, "signal-B")
        output = _require_signal(routed, entity.identifier, 2, "signal-C")
        behavior = FactorioArithmeticControlBehavior(
            FactorioBlueprintSignal(first.signal),
            FactorioBlueprintSignal(second.signal),
            FactorioBlueprintSignal(output.signal),
            FactorioNetworkSelection.from_color(first.color),
            FactorioNetworkSelection.from_color(second.color),
        )
        direction = _factorio_direction(entity.angle)
    elif entity.prototype == "small-lamp":
        configuration = FactorioObjectConfigurationCodec().decode(
            FACTORIO_LAMP,
            entity.configuration,
        )
        if not isinstance(configuration, FactorioLampConfiguration):
            raise FactorioBlueprintError(
                f"Lamp {entity.identifier!r} has invalid configuration"
            )
        assignment = _require_incident_signal(routed, entity.identifier)
        behavior = FactorioLampControlBehavior(
            FactorioBlueprintSignal(assignment.signal),
            configuration.comparator,
            configuration.constant,
        )
        direction = None
    elif entity.prototype in {"medium-electric-pole", "big-electric-pole"}:
        behavior = None
        direction = None
    else:
        raise FactorioBlueprintError(
            f"Unsupported Factorio blueprint prototype {entity.prototype!r}"
        )
    return FactorioBlueprintEntity(
        entity_number,
        entity.prototype,
        entity.x,
        entity.y,
        direction,
        behavior,
    )


def _require_signal(
    routed: FactorioRoutedDesign,
    entity: str,
    connector: int,
    signal: str,
) -> FactorioSignalAssignment:
    assignment_by_net = {item.net: item for item in routed.signal_assignments}
    matches = {
        assignment_by_net[wire.net]
        for wire in routed.wires
        if wire.net in assignment_by_net
        and (
            (wire.source.entity == entity and wire.source.connector == connector)
            or (wire.target.entity == entity and wire.target.connector == connector)
        )
        and assignment_by_net[wire.net].signal == signal
    }
    if len(matches) != 1:
        raise FactorioBlueprintError(
            f"Factorio entity {entity!r} connector {connector} has "
            f"{len(matches)} assignments for {signal!r}"
        )
    return next(iter(matches))


def _require_incident_signal(
    routed: FactorioRoutedDesign,
    entity: str,
) -> FactorioSignalAssignment:
    assignment_by_net = {item.net: item for item in routed.signal_assignments}
    matches = {
        assignment_by_net[wire.net]
        for wire in routed.wires
        if wire.net in assignment_by_net
        and entity in {wire.source.entity, wire.target.entity}
    }
    if len(matches) != 1:
        raise FactorioBlueprintError(
            f"Factorio lamp {entity!r} has {len(matches)} incident signal assignments"
        )
    return next(iter(matches))


def _blueprint_connector(
    routed: FactorioRoutedDesign,
    entity_id: str,
    connector: int,
    color: FactorioWireColor,
) -> int:
    entity = next(
        (item for item in routed.entities if item.identifier == entity_id),
        None,
    )
    if entity is None:
        raise FactorioBlueprintError(
            f"Factorio wire references unknown entity {entity_id!r}"
        )
    return _connector_number(entity.prototype, connector, color)


def _connector_number(prototype: str, connector: int, color: FactorioWireColor) -> int:
    color_offset = 0 if color == FactorioWireColor.RED else 1
    if prototype in {"arithmetic-combinator", "decider-combinator"}:
        if connector == 1:
            return 1 + color_offset
        if connector == 2:
            return 3 + color_offset
        raise FactorioBlueprintError(
            f"Arithmetic combinator has unsupported connector {connector}"
        )
    if connector != 1:
        raise FactorioBlueprintError(
            f"Factorio entity has unsupported connector {connector}"
        )
    return 1 + color_offset


def _factorio_direction(angle: float) -> int:
    normalized = angle % 360.0
    directions = {
        0.0: 4,
        90.0: 8,
        180.0: 12,
        270.0: 0,
    }
    for expected, direction in directions.items():
        if math.isclose(normalized, expected, abs_tol=1e-9):
            return direction
    raise FactorioBlueprintError(
        f"Factorio blueprint does not support entity angle {angle:g}"
    )