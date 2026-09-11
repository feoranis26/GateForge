from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
import hashlib
import json
import math
import re

from gateforge.material import MaterialDesignDigest, MaterialNetId
from gateforge.providers.factorio.profile import factorio_entity_profile
from gateforge.target import ProviderConfiguration


ROUTED_SCHEMA_VERSION = 3
_SHA256 = re.compile(r"[0-9a-f]{64}")


class FactorioRoutingError(ValueError):
    pass


class FactorioWireColor(StrEnum):
    RED = "red"
    GREEN = "green"


class FactorioInputDriverMode(StrEnum):
    NONE = "none"
    CONSTANT = "constant"


@dataclass(frozen=True, slots=True)
class FactorioEntity:
    identifier: str
    prototype: str
    x: float
    y: float
    angle: float = 0.0
    source_component: str | None = None
    configuration: ProviderConfiguration = ProviderConfiguration()

    def __post_init__(self) -> None:
        if not self.identifier:
            raise FactorioRoutingError("Factorio entity identifier must not be empty")
        factorio_entity_profile(self.prototype)
        for attribute in ("x", "y", "angle"):
            value = getattr(self, attribute)
            if (
                isinstance(value, bool)
                or not isinstance(value, (int, float))
                or not math.isfinite(float(value))
            ):
                raise FactorioRoutingError(
                    f"Factorio entity {attribute} must be finite"
                )
            object.__setattr__(self, attribute, float(value))


@dataclass(frozen=True, slots=True, order=True)
class FactorioConnectorEndpoint:
    entity: str
    connector: int

    def __post_init__(self) -> None:
        if not self.entity:
            raise FactorioRoutingError("Factorio wire endpoint entity must not be empty")
        if (
            not isinstance(self.connector, int)
            or isinstance(self.connector, bool)
            or self.connector <= 0
        ):
            raise FactorioRoutingError(
                "Factorio wire connector must be a positive integer"
            )


@dataclass(frozen=True, slots=True)
class FactorioWireSegment:
    net: MaterialNetId
    color: FactorioWireColor
    source: FactorioConnectorEndpoint
    target: FactorioConnectorEndpoint

    def __post_init__(self) -> None:
        if self.source == self.target:
            raise FactorioRoutingError("Factorio wire segment cannot be a self-loop")
        if self.target < self.source:
            source = self.source
            object.__setattr__(self, "source", self.target)
            object.__setattr__(self, "target", source)


@dataclass(frozen=True, slots=True, order=True)
class FactorioPowerSegment:
    source: str
    target: str

    def __post_init__(self) -> None:
        if not self.source or not self.target:
            raise FactorioRoutingError(
                "Factorio power-segment entity names must not be empty"
            )
        if self.source == self.target:
            raise FactorioRoutingError("Factorio power segment cannot be a self-loop")
        if self.target < self.source:
            source = self.source
            object.__setattr__(self, "source", self.target)
            object.__setattr__(self, "target", source)


@dataclass(frozen=True, slots=True)
class FactorioSignalAssignment:
    net: MaterialNetId
    signal: str
    color: FactorioWireColor

    def __post_init__(self) -> None:
        if not self.signal:
            raise FactorioRoutingError("Factorio signal name must not be empty")


@dataclass(frozen=True, slots=True)
class FactorioInputDriver:
    port: str
    net: MaterialNetId
    entity: str
    unsigned_value: int
    signed_count: int

    def __post_init__(self) -> None:
        if not self.port or not self.entity:
            raise FactorioRoutingError("Factorio input driver names must not be empty")
        if not 0 <= self.unsigned_value < (1 << 32):
            raise FactorioRoutingError("Factorio input value must fit in 32 bits")
        expected = (
            self.unsigned_value
            if self.unsigned_value < (1 << 31)
            else self.unsigned_value - (1 << 32)
        )
        if self.signed_count != expected:
            raise FactorioRoutingError(
                "Factorio input driver signed count does not match its value"
            )


@dataclass(frozen=True, slots=True)
class FactorioOutputLamp:
    port: str
    net: MaterialNetId
    entity: str
    terminal_entity: str
    signal: str
    color: FactorioWireColor

    def __post_init__(self) -> None:
        if not self.port or not self.entity or not self.terminal_entity:
            raise FactorioRoutingError("Factorio output lamp names must not be empty")
        if not self.signal:
            raise FactorioRoutingError("Factorio output lamp signal must not be empty")


@dataclass(frozen=True, slots=True)
class FactorioRoutedDesign:
    material_digest: MaterialDesignDigest
    placement_digest: str
    entities: tuple[FactorioEntity, ...]
    signal_assignments: tuple[FactorioSignalAssignment, ...]
    wires: tuple[FactorioWireSegment, ...]
    power_segments: tuple[FactorioPowerSegment, ...] = ()
    input_drivers: tuple[FactorioInputDriver, ...] = ()
    output_lamps: tuple[FactorioOutputLamp, ...] = ()

    def __post_init__(self) -> None:
        if _SHA256.fullmatch(self.placement_digest) is None:
            raise FactorioRoutingError(
                "Factorio routed placement digest must be a SHA-256 value"
            )
        entities = tuple(sorted(self.entities, key=lambda item: item.identifier))
        assignments = tuple(
            sorted(self.signal_assignments, key=lambda item: item.net.value)
        )
        wires = tuple(
            sorted(
                self.wires,
                key=lambda item: (
                    item.net.value,
                    item.color.value,
                    item.source,
                    item.target,
                ),
            )
        )
        power_segments = tuple(sorted(self.power_segments))
        drivers = tuple(sorted(self.input_drivers, key=lambda item: item.port))
        lamps = tuple(sorted(self.output_lamps, key=lambda item: item.port))
        _require_unique((item.identifier for item in entities), "entity")
        _require_unique((item.net for item in assignments), "signal assignment")
        _require_unique(wires, "wire segment")
        _require_unique(power_segments, "power segment")
        _require_unique((item.port for item in drivers), "input driver port")
        _require_unique((item.port for item in lamps), "output lamp port")
        _require_unique((item.entity for item in lamps), "output lamp entity")
        entity_by_id = {item.identifier: item for item in entities}
        assignment_by_net = {item.net: item for item in assignments}
        _validate_collisions(entities)
        connector_colors: dict[
            tuple[FactorioConnectorEndpoint, FactorioWireColor], MaterialNetId
        ] = {}
        for wire in wires:
            assignment = assignment_by_net.get(wire.net)
            if assignment is None or assignment.color != wire.color:
                raise FactorioRoutingError(
                    f"Factorio wire for {wire.net.value} lacks a matching signal "
                    "assignment"
                )
            for endpoint in (wire.source, wire.target):
                entity = entity_by_id.get(endpoint.entity)
                if entity is None:
                    raise FactorioRoutingError(
                        f"Factorio wire references unknown entity {endpoint.entity!r}"
                    )
                _connector(entity, endpoint.connector)
                key = (endpoint, wire.color)
                existing = connector_colors.get(key)
                if existing is not None and existing != wire.net:
                    raise FactorioRoutingError(
                        f"Factorio {wire.color.value} connector {endpoint} mixes "
                        "logical nets"
                    )
                connector_colors[key] = wire.net
            distance = connector_distance(
                entity_by_id[wire.source.entity],
                wire.source.connector,
                entity_by_id[wire.target.entity],
                wire.target.connector,
            )
            reach = min(
                factorio_entity_profile(
                    entity_by_id[wire.source.entity].prototype
                ).circuit_wire_reach,
                factorio_entity_profile(
                    entity_by_id[wire.target.entity].prototype
                ).circuit_wire_reach,
            )
            if distance > reach + 1e-9:
                raise FactorioRoutingError(
                    f"Factorio wire on net {wire.net.value} spans {distance:.3f} "
                    f"tiles, exceeding reach {reach:.3f}"
                )
        for segment in power_segments:
            try:
                source = entity_by_id[segment.source]
                target = entity_by_id[segment.target]
            except KeyError as error:
                raise FactorioRoutingError(
                    "Factorio power segment references an unknown entity"
                ) from error
            source_reach = factorio_entity_profile(
                source.prototype
            ).copper_wire_reach
            target_reach = factorio_entity_profile(
                target.prototype
            ).copper_wire_reach
            if source_reach is None or target_reach is None:
                raise FactorioRoutingError(
                    "Factorio power segments may connect electric poles only"
                )
            distance = math.hypot(target.x - source.x, target.y - source.y)
            reach = min(source_reach, target_reach)
            if distance > reach + 1e-9:
                raise FactorioRoutingError(
                    f"Factorio power segment spans {distance:.3f} tiles, "
                    f"exceeding reach {reach:.3f}"
                )
        for driver in drivers:
            entity = entity_by_id.get(driver.entity)
            if entity is None or entity.prototype != "constant-combinator":
                raise FactorioRoutingError(
                    f"Factorio input driver {driver.port!r} has no constant "
                    "combinator entity"
                )
            if driver.net not in assignment_by_net:
                raise FactorioRoutingError(
                    f"Factorio input driver {driver.port!r} has no signal assignment"
                )
        for lamp in lamps:
            entity = entity_by_id.get(lamp.entity)
            if entity is None or entity.prototype != "small-lamp":
                raise FactorioRoutingError(
                    f"Factorio output lamp {lamp.port!r} has no small-lamp entity"
                )
            if lamp.terminal_entity not in entity_by_id:
                raise FactorioRoutingError(
                    f"Factorio output lamp {lamp.port!r} has no terminal entity"
                )
            assignment = assignment_by_net.get(lamp.net)
            if (
                assignment is None
                or assignment.signal != lamp.signal
                or assignment.color != lamp.color
            ):
                raise FactorioRoutingError(
                    f"Factorio output lamp {lamp.port!r} has no matching signal assignment"
                )
            incident = tuple(
                wire
                for wire in wires
                if lamp.entity in {wire.source.entity, wire.target.entity}
            )
            if len(incident) != 1 or incident[0].net != lamp.net:
                raise FactorioRoutingError(
                    f"Factorio output lamp {lamp.port!r} must have one routed input"
                )
        object.__setattr__(self, "entities", entities)
        object.__setattr__(self, "signal_assignments", assignments)
        object.__setattr__(self, "wires", wires)
        object.__setattr__(self, "power_segments", power_segments)
        object.__setattr__(self, "input_drivers", drivers)
        object.__setattr__(self, "output_lamps", lamps)

    def canonical_data(self) -> dict[str, object]:
        return {
            "schema_version": ROUTED_SCHEMA_VERSION,
            "material_digest": self.material_digest.value,
            "placement_digest": self.placement_digest,
            "entities": [
                {
                    "id": item.identifier,
                    "prototype": item.prototype,
                    "position": {"x": item.x, "y": item.y},
                    "angle": item.angle,
                    "source_component": item.source_component,
                    "configuration": item.configuration.canonical_data(),
                }
                for item in self.entities
            ],
            "signals": [
                {
                    "net": item.net.value,
                    "signal": item.signal,
                    "color": item.color.value,
                }
                for item in self.signal_assignments
            ],
            "wires": [
                {
                    "net": item.net.value,
                    "color": item.color.value,
                    "source": {
                        "entity": item.source.entity,
                        "connector": item.source.connector,
                    },
                    "target": {
                        "entity": item.target.entity,
                        "connector": item.target.connector,
                    },
                }
                for item in self.wires
            ],
            "power_segments": [
                {"source": item.source, "target": item.target}
                for item in self.power_segments
            ],
            "input_drivers": [
                {
                    "port": item.port,
                    "net": item.net.value,
                    "entity": item.entity,
                    "unsigned_value": item.unsigned_value,
                    "signed_count": item.signed_count,
                }
                for item in self.input_drivers
            ],
            "output_lamps": [
                {
                    "port": item.port,
                    "net": item.net.value,
                    "entity": item.entity,
                    "terminal_entity": item.terminal_entity,
                    "signal": item.signal,
                    "color": item.color.value,
                }
                for item in self.output_lamps
            ],
        }

    def get_digest(self) -> str:
        encoded = json.dumps(
            self.canonical_data(),
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()


def connector_position(
    entity: FactorioEntity,
    connector: int,
) -> tuple[float, float]:
    item = _connector(entity, connector)
    radians = math.radians(entity.angle)
    cosine = math.cos(radians)
    sine = math.sin(radians)
    return (
        entity.x + item.x * cosine - item.y * sine,
        entity.y + item.x * sine + item.y * cosine,
    )


def connector_distance(
    left: FactorioEntity,
    left_connector: int,
    right: FactorioEntity,
    right_connector: int,
) -> float:
    left_x, left_y = connector_position(left, left_connector)
    right_x, right_y = connector_position(right, right_connector)
    return math.hypot(right_x - left_x, right_y - left_y)


def entity_bounds(entity: FactorioEntity) -> tuple[float, float, float, float]:
    profile = factorio_entity_profile(entity.prototype)
    quarter_turn = round(entity.angle / 90.0) % 2
    width = profile.height if quarter_turn else profile.width
    height = profile.width if quarter_turn else profile.height
    return (
        entity.x - width / 2,
        entity.y - height / 2,
        entity.x + width / 2,
        entity.y + height / 2,
    )


def entities_collide(left: FactorioEntity, right: FactorioEntity) -> bool:
    left_min_x, left_min_y, left_max_x, left_max_y = entity_bounds(left)
    right_min_x, right_min_y, right_max_x, right_max_y = entity_bounds(right)
    tolerance = 1e-9
    return (
        left_min_x < right_max_x - tolerance
        and left_max_x > right_min_x + tolerance
        and left_min_y < right_max_y - tolerance
        and left_max_y > right_min_y + tolerance
    )


def _connector(entity: FactorioEntity, connector: int):
    profile = factorio_entity_profile(entity.prototype)
    try:
        return next(item for item in profile.connectors if item.identifier == connector)
    except StopIteration as error:
        raise FactorioRoutingError(
            f"Factorio entity {entity.identifier!r} has no connector {connector}"
        ) from error


def _validate_collisions(entities: tuple[FactorioEntity, ...]) -> None:
    for index, left in enumerate(entities):
        for right in entities[index + 1 :]:
            if entities_collide(left, right):
                raise FactorioRoutingError(
                    f"Factorio entities {left.identifier!r} and "
                    f"{right.identifier!r} overlap"
                )


def _require_unique(values, context: str) -> None:
    seen = set()
    for value in values:
        if value in seen:
            raise FactorioRoutingError(f"Duplicate Factorio {context} {value!r}")
        seen.add(value)