from __future__ import annotations

from collections import Counter, deque
from collections.abc import Mapping
from dataclasses import dataclass, replace
import hashlib
import heapq
import json
import math

from gateforge.behavior import BehaviorGraph
from gateforge.material import MaterialDesign
from gateforge.providers.factorio.blueprint import (
    FactorioArithmeticControlBehavior,
    FactorioDeciderControlBehavior,
    FactorioBlueprintSignal,
    FactorioConstantControlBehavior,
    FactorioConstantFilter,
    FactorioLampControlBehavior,
    FactorioNetworkSelection,
)
from gateforge.providers.factorio.profile import factorio_entity_profile
from gateforge.providers.factorio.realization import (
    FactorioCircuitDomain,
    FactorioCircuitFabric,
    FactorioConstantRead,
    FactorioDomainWire,
)
from gateforge.providers.factorio.routed import (
    FactorioConnectorEndpoint,
    FactorioEntity,
    FactorioInputDriverMode,
    FactorioPowerSegment,
    FactorioRoutingError,
    FactorioWireColor,
    connector_distance,
    entities_collide,
    entity_bounds,
)
from gateforge.providers.factorio.routing import (
    _snap_entity_position,
    parse_factorio_input_value,
    route_connector_link,
)
from gateforge.realization import (
    RealizationDesign,
    RealizationError,
    RealizationInfeasibleError,
    RealizationProvenance,
    RealizedEndpoint,
    RealizedEntity,
    RealizedEntityPlacement,
    RealizedPlacement,
)
from gateforge.target import ProviderConfiguration


_POLES = {"medium-electric-pole", "big-electric-pole"}
_POWERED = {"arithmetic-combinator", "decider-combinator", "small-lamp"}


def physical_connector(endpoint: RealizedEndpoint, entity: FactorioEntity) -> FactorioConnectorEndpoint:
    if entity.prototype in {"arithmetic-combinator", "decider-combinator"}:
        if endpoint.port not in {"in", "out"}:
            raise RealizationError("Arithmetic endpoint must be in or out")
        connector = 1 if endpoint.port == "in" else 2
    else:
        if endpoint.port not in {"out", "circuit"}:
            raise RealizationError("Single-connector endpoint must be out or circuit")
        connector = 1
    return FactorioConnectorEndpoint(endpoint.entity, connector)


def _covered(entity: FactorioEntity, pole: FactorioEntity) -> bool:
    radius = 3.5 if pole.prototype == "medium-electric-pole" else 2.0
    left, top, right, bottom = entity_bounds(entity)
    return left >= pole.x - radius and right <= pole.x + radius and top >= pole.y - radius and bottom <= pole.y + radius


def _supply_candidates(entities: Mapping[str, FactorioEntity], origin: tuple[float, float] | None = None) -> dict[tuple[float, float], FactorioEntity]:
    candidates = {}
    step = 1 if origin is None else 4
    offset_x, offset_y = (0.5, 0.5) if origin is None else origin
    for entity in entities.values():
        if entity.prototype not in _POWERED:
            continue
        for column in range(math.ceil((entity.x - 3 - offset_x) / step), math.floor((entity.x + 3 - offset_x) / step) + 1):
            for row in range(math.ceil((entity.y - 3 - offset_y) / step), math.floor((entity.y + 3 - offset_y) / step) + 1):
                x, y = offset_x + column * step, offset_y + row * step
                candidate = FactorioEntity(f"power:supply:{x:g}:{y:g}", "medium-electric-pole", x, y)
                if _covered(entity, candidate) and not any(entities_collide(candidate, other) for other in entities.values()):
                    candidates[x, y] = candidate
    return candidates


def _choose_supply(entities: Mapping[str, FactorioEntity], candidates: Mapping[tuple[float, float], FactorioEntity]) -> tuple[FactorioEntity, ...]:
    uncovered = {item.identifier: item for item in entities.values() if item.prototype in _POWERED and not any(_covered(item, pole) for pole in entities.values() if pole.prototype in _POLES)}
    selected = []
    while uncovered:
        if not candidates:
            raise RealizationInfeasibleError("Cannot place collision-free supply poles")
        pole = min(candidates.values(), key=lambda pole: (
            -sum(_covered(item, pole) for item in uncovered.values()),
            sum(math.hypot(item.x - pole.x, item.y - pole.y) for item in uncovered.values() if _covered(item, pole)),
            pole.x, pole.y,
        ))
        supplied = {identifier for identifier, item in uncovered.items() if _covered(item, pole)}
        if not supplied:
            raise RealizationInfeasibleError("Cannot cover powered entities without collisions")
        selected.append(pole)
        uncovered = {identifier: item for identifier, item in uncovered.items() if identifier not in supplied}
    for pole in tuple(reversed(selected)):
        others = [item for item in entities.values() if item.prototype in _POLES] + [item for item in selected if item != pole]
        if all(any(_covered(item, other) for other in others) for item in entities.values() if item.prototype in _POWERED):
            selected.remove(pole)
    return tuple(selected)


def _existing_pole_route(source: FactorioConnectorEndpoint, target: FactorioConnectorEndpoint, entities: Mapping[str, FactorioEntity], available: set[str]) -> tuple[tuple[FactorioConnectorEndpoint, FactorioConnectorEndpoint], ...] | None:
    endpoints = {source, target, *(FactorioConnectorEndpoint(identifier, 1) for identifier in available)}
    pending = [(0.0, source)]
    distances = {source: 0.0}
    parents = {}
    while pending:
        distance, endpoint = heapq.heappop(pending)
        if distance != distances[endpoint]:
            continue
        if endpoint == target:
            links = []
            while endpoint != source:
                parent = parents[endpoint]
                links.append((parent, endpoint))
                endpoint = parent
            return tuple(reversed(links))
        for neighbor in sorted(endpoints - {endpoint}):
            length = connector_distance(entities[endpoint.entity], endpoint.connector, entities[neighbor.entity], neighbor.connector)
            reach = min(factorio_entity_profile(entities[item.entity].prototype).circuit_wire_reach for item in (endpoint, neighbor))
            cost = distance + length + 0.001
            if length <= reach + 1e-9 and cost < distances.get(neighbor, math.inf):
                distances[neighbor] = cost
                parents[neighbor] = endpoint
                heapq.heappush(pending, (cost, neighbor))
    return None


def _place_supply(entities: dict[str, FactorioEntity], power_layout: str) -> tuple[float, float] | None:
    origin = None
    if power_layout == "grid":
        layouts = []
        for offset_x in (0.5, 1.5, 2.5, 3.5):
            for offset_y in (0.5, 1.5, 2.5, 3.5):
                try:
                    supply = _choose_supply(entities, _supply_candidates(entities, (offset_x, offset_y)))
                except RealizationInfeasibleError:
                    continue
                off_grid = sum((item.x - offset_x) % 4 != 0 or (item.y - offset_y) % 4 != 0 for item in entities.values() if item.prototype in _POLES)
                layouts.append((len(supply), off_grid, offset_x, offset_y, supply))
        if not layouts:
            raise RealizationInfeasibleError("No collision-free supply grid covers the circuit")
        _, _, offset_x, offset_y, supply = min(layouts, key=lambda item: item[:4])
        origin = (offset_x, offset_y)
    else:
        supply = _choose_supply(entities, _supply_candidates(entities))
    for pole in supply:
        entities[pole.identifier] = pole
    return origin


def _addition_configuration(addition) -> ProviderConfiguration:
    def selection(read):
        if isinstance(read, FactorioConstantRead):
            return FactorioNetworkSelection(False, False)
        return FactorioNetworkSelection(
            FactorioWireColor.RED in read.colors,
            FactorioWireColor.GREEN in read.colors,
        )

    if addition.prototype == "decider-combinator":
        if isinstance(addition.first, FactorioConstantRead):
            raise RealizationError("Decider first operand must be a signal")
        return ProviderConfiguration.from_canonical_data(FactorioDeciderControlBehavior(
            FactorioBlueprintSignal(addition.first.signal),
            addition.second.value if isinstance(addition.second, FactorioConstantRead) else FactorioBlueprintSignal(addition.second.signal),
            FactorioBlueprintSignal(addition.output_signal), selection(addition.first), selection(addition.second), addition.operation,
        ).canonical_data())
    return ProviderConfiguration.from_canonical_data(FactorioArithmeticControlBehavior(
        addition.first.value if isinstance(addition.first, FactorioConstantRead) else FactorioBlueprintSignal(addition.first.signal),
        addition.second.value if isinstance(addition.second, FactorioConstantRead) else FactorioBlueprintSignal(addition.second.signal),
        FactorioBlueprintSignal(addition.output_signal),
        selection(addition.first), selection(addition.second), addition.operation,
    ).canonical_data())


def _grid_power(entities: dict[str, FactorioEntity], origin: tuple[float, float]) -> tuple[FactorioPowerSegment, ...]:
    offset_x, offset_y = origin
    existing = tuple(item for item in entities.values() if item.prototype in _POLES)
    if existing:
        connected = {min(item.identifier for item in existing)}
        remaining = {item.identifier for item in existing} - connected
        direct: list[FactorioPowerSegment] = []
        degrees: Counter[str] = Counter()
        while remaining:
            choices = [
                (math.hypot(entities[source].x - entities[target].x, entities[source].y - entities[target].y), source, target)
                for source in sorted(connected) if degrees[source] < 5 for target in sorted(remaining)
                if math.hypot(entities[source].x - entities[target].x, entities[source].y - entities[target].y)
                <= min(factorio_entity_profile(entities[item].prototype).copper_wire_reach for item in (source, target))
            ]
            if not choices:
                break
            _, source, target = min(choices)
            direct.append(FactorioPowerSegment(source, target))
            degrees[source] += 1
            degrees[target] += 1
            connected.add(target)
            remaining.remove(target)
        if not remaining:
            return tuple(sorted(direct))
    nodes: dict[tuple[int, int], FactorioEntity] = {}
    drops: set[tuple[int, int]] = set()
    power: set[FactorioPowerSegment] = set()

    def site(coordinate):
        if coordinate in nodes:
            return nodes[coordinate]
        column, row = coordinate
        x, y = offset_x + 4 * column, offset_y + 4 * row
        pole = next((item for item in existing if item.prototype == "medium-electric-pole" and (item.x, item.y) == (x, y)), None)
        if pole is not None:
            return pole
        candidate = FactorioEntity(f"power:grid:{column}:{row}", "medium-electric-pole", x, y)
        return None if any(entities_collide(candidate, item) for item in entities.values()) else candidate

    for pole in existing:
        column = round((pole.x - offset_x) / 4)
        row = round((pole.y - offset_y) / 4)
        if pole.prototype == "medium-electric-pole" and (pole.x, pole.y) == (offset_x + 4 * column, offset_y + 4 * row):
            nodes[column, row] = pole
            continue
        choices = []
        for grid_x in range(column - 2, column + 3):
            for grid_y in range(row - 2, row + 3):
                coordinate = (grid_x, grid_y)
                candidate = site(coordinate)
                if candidate is None or coordinate in drops:
                    continue
                distance = math.hypot(candidate.x - pole.x, candidate.y - pole.y)
                if distance <= 9:
                    choices.append((distance, coordinate, candidate))
        if not choices:
            raise RealizationInfeasibleError("Cannot attach a circuit pole to the power grid")
        _, coordinate, anchor = min(choices, key=lambda item: (item[0], item[1]))
        nodes[coordinate] = anchor
        entities[anchor.identifier] = anchor
        if anchor.identifier != pole.identifier:
            drops.add(coordinate)
            power.add(FactorioPowerSegment(anchor.identifier, pole.identifier))

    anchors = set(nodes)
    connected = {min(anchors)}
    pending = anchors - connected
    min_column, max_column = min(item[0] for item in anchors) - 3, max(item[0] for item in anchors) + 3
    min_row, max_row = min(item[1] for item in anchors) - 3, max(item[1] for item in anchors) + 3
    while pending:
        target = min(pending, key=lambda item: (min(abs(item[0] - other[0]) + abs(item[1] - other[1]) for other in connected), item))
        queue = deque([target])
        parents = {target: None}
        reached = None
        while queue and len(parents) <= 100000:
            current = queue.popleft()
            if current in connected:
                reached = current
                break
            column, row = current
            for neighbor in ((column - 1, row), (column, row - 1), (column, row + 1), (column + 1, row)):
                if neighbor in parents or not (min_column <= neighbor[0] <= max_column and min_row <= neighbor[1] <= max_row) or site(neighbor) is None:
                    continue
                parents[neighbor] = current
                queue.append(neighbor)
        if reached is None:
            raise RealizationInfeasibleError("Cannot connect the collision-free power grid within routing bounds")
        current = reached
        while parents[current] is not None:
            neighbor = parents[current]
            pole = site(neighbor)
            assert pole is not None
            nodes[neighbor] = pole
            entities[pole.identifier] = pole
            power.add(FactorioPowerSegment(nodes[current].identifier, pole.identifier))
            connected.add(neighbor)
            current = neighbor
        pending -= connected
    protected = {item.identifier for item in existing}
    for pole in tuple(nodes.values()):
        if pole.identifier in protected:
            continue
        edges = [segment for segment in power if pole.identifier in (segment.source, segment.target)]
        if len(edges) != 2:
            continue
        neighbors = [entities[segment.target if segment.source == pole.identifier else segment.source] for segment in edges]
        first, second = neighbors
        if (first.x != second.x and first.y != second.y) or math.hypot(first.x - second.x, first.y - second.y) > 9:
            continue
        others = [item for item in entities.values() if item.prototype in _POLES and item.identifier != pole.identifier]
        if not all(any(_covered(item, other) for other in others) for item in entities.values() if item.prototype in _POWERED):
            continue
        power.difference_update(edges)
        power.add(FactorioPowerSegment(first.identifier, second.identifier))
        del entities[pole.identifier]
    return tuple(sorted(power))


@dataclass(frozen=True, slots=True)
class FactorioFinalizedDesign:
    fabric: FactorioCircuitFabric
    design: RealizationDesign
    placement: RealizedPlacement
    domains: tuple[FactorioCircuitDomain, ...]
    wires: tuple[FactorioDomainWire, ...]
    power_segments: tuple[FactorioPowerSegment, ...]
    external_supply: str
    settling_ticks: int
    power_layout: str = "compact"

    def __post_init__(self) -> None:
        if self.power_layout not in {"grid", "compact"}:
            raise RealizationError("Power layout must be grid or compact")
        for items in (self.domains, self.wires, self.power_segments):
            if not isinstance(items, tuple) or len(set(items)) != len(items):
                raise RealizationError("Finalized collections must be unique immutable tuples")
        if isinstance(self.settling_ticks, bool) or not isinstance(self.settling_ticks, int) or self.settling_ticks < 0:
            raise RealizationError("Settling bound must be a nonnegative integer")
        self.validate_physical()

    @property
    def target(self) -> str:
        return self.design.target

    @property
    def entities(self) -> tuple[FactorioEntity, ...]:
        inventory = {item.identifier: item for item in self.design.entities}
        return tuple(
            FactorioEntity(
                item.entity, inventory[item.entity].kind, item.x, item.y, item.angle,
                configuration=inventory[item.entity].configuration,
            )
            for item in sorted(self.placement.entities, key=lambda item: item.entity)
        )

    def validate(self, material: MaterialDesign, behavior: BehaviorGraph) -> None:
        self.fabric.validate(material, behavior)
        self.design.validate(material, behavior)
        self.validate_physical()

    def validate_physical(self) -> None:
        self.placement.validate(self.design)
        if self.target != "factorio" or self.design.material_digest != self.fabric.design.material_digest or self.design.behavior_digest != self.fabric.design.behavior_digest:
            raise RealizationError("Physical inventory does not match its source fabric")
        if self.design.observations != self.fabric.design.observations:
            raise RealizationError("Physical observations differ from source fabric")
        if self.settling_ticks != self.fabric.analyze().settling_ticks:
            raise RealizationError("Physical settling bound differs from circuit analysis")
        logical = {item.identifier: item for item in self.fabric.design.entities}
        inventory = {item.identifier: item for item in self.design.entities}
        if not logical.keys() <= inventory.keys():
            raise RealizationError("Physical inventory is missing a circuit entity")
        for identifier, entity in inventory.items():
            original = logical.get(identifier)
            if original is None:
                if entity.kind not in _POLES or entity.ports != ("circuit",) or not entity.configuration.is_empty:
                    raise RealizationError("Derived inventory must contain unconfigured poles only")
            else:
                allowed = {
                    "input-interface": {"constant-combinator", "medium-electric-pole"},
                    "junction": {"small-lamp", "medium-electric-pole"},
                    "arithmetic-combinator": {"arithmetic-combinator"},
                    "decider-combinator": {"decider-combinator"},
                }[original.kind]
                if entity.kind not in allowed or set(entity.ports) != set(original.ports):
                    raise RealizationError("Physical entity does not implement its circuit role")
        entities = self.entities
        by_id = {item.identifier: item for item in entities}
        for index, entity in enumerate(entities):
            if entity.angle not in {0.0, 90.0, 180.0, 270.0}:
                raise RealizationError("Physical entity angle must be a quarter turn")
            if _snap_entity_position(entity.prototype, entity.x, entity.y, entity.angle) != (entity.x, entity.y):
                raise RealizationError("Physical entity is not aligned to the tile grid")
            if any(entities_collide(entity, other) for other in entities[index + 1:]):
                raise RealizationError("Physical entities overlap")
            if entity.prototype in _POLES and not entity.configuration.is_empty:
                raise RealizationError("Poles cannot have combinator configuration")
        for addition in self.fabric.additions:
            if inventory[addition.entity].configuration != _addition_configuration(addition):
                raise RealizationError("Physical arithmetic configuration differs from circuit")
        for identifier in {item.endpoint.entity for item in self.fabric.inputs}:
            entity = inventory[identifier]
            if entity.kind == "constant-combinator":
                try:
                    filters = entity.configuration.canonical_data()["sections"]["sections"][0]["filters"]
                    counts = {item["name"]: item["count"] for item in filters}
                    emissions = sorted((item for item in self.fabric.inputs if item.endpoint.entity == identifier), key=lambda item: item.signal)
                    if any(emission.width == 1 and counts[emission.signal] not in {0, 1} for emission in emissions):
                        raise RealizationError("Boolean input driver must emit 0 or 1")
                    expected = FactorioConstantControlBehavior(tuple(FactorioConstantFilter(
                        FactorioBlueprintSignal(emission.signal), counts[emission.signal], index,
                    ) for index, emission in enumerate(emissions, start=1))).canonical_data()
                except (KeyError, IndexError, TypeError, ValueError) as error:
                    raise RealizationError("Invalid physical input driver configuration") from error
                if entity.configuration.canonical_data() != expected:
                    raise RealizationError("Physical input driver must emit exactly its declared bus signals")
        observed_entities = {item.read.endpoint.entity for item in self.fabric.observations}
        for original in self.fabric.design.entities:
            entity = inventory[original.identifier]
            native_lamp = original.configuration.canonical_data().get("native_lamp", False)
            if native_lamp and entity.kind != "small-lamp":
                raise RealizationError("Native lamp must survive physical finalization")
            if entity.kind == "small-lamp" and entity.identifier not in observed_entities:
                raise RealizationError("Physical lamp lacks an observation")
        for observation in self.fabric.observations:
            entity = inventory[observation.read.endpoint.entity]
            if entity.kind == "small-lamp":
                expected = ProviderConfiguration.from_canonical_data(FactorioLampControlBehavior(
                    FactorioBlueprintSignal(observation.read.signal)
                ).canonical_data())
                if entity.configuration != expected:
                    raise RealizationError("Physical lamp configuration differs from observation")
                both = replace(observation.read, colors=tuple(FactorioWireColor))
                if Counter(self.fabric.drivers(both)) != Counter(self.fabric.drivers(observation.read)):
                    raise RealizationError("Lamp would read an unselected color")

        original_endpoints = {RealizedEndpoint(item.identifier, port) for item in logical.values() for port in item.ports}
        original_domains = {item.identifier: item for item in self.fabric.domains}
        if {item.identifier for item in self.domains} != original_domains.keys():
            raise RealizationError("Physical domain identities differ from circuit")
        for domain in self.domains:
            original = original_domains[domain.identifier]
            if domain.color != original.color or set(domain.endpoints) & original_endpoints != set(original.endpoints):
                raise RealizationError("Physical domain merges or drops circuit endpoints")
        projected = replace(self.fabric.design, entities=(
            *self.fabric.design.entities,
            *(RealizedEntity(item.identifier, "junction", ("circuit",)) for item in self.design.entities if item.identifier not in logical),
        ))
        replace(self.fabric, design=projected, domains=self.domains, wires=self.wires)
        for wire in self.wires:
            source = physical_connector(wire.source, by_id[wire.source.entity])
            target = physical_connector(wire.target, by_id[wire.target.entity])
            reach = min(factorio_entity_profile(by_id[endpoint.entity].prototype).circuit_wire_reach for endpoint in (source, target))
            if connector_distance(by_id[source.entity], source.connector, by_id[target.entity], target.connector) > reach + 1e-9:
                raise RealizationError("Physical circuit wire exceeds reach")
        poles = {item.identifier: item for item in entities if item.prototype in _POLES}
        if self.external_supply not in poles:
            raise RealizationError("External supply must name a physical pole")
        adjacency = {identifier: set() for identifier in poles}
        for segment in self.power_segments:
            if segment.source not in poles or segment.target not in poles:
                raise RealizationError("Copper wires may connect poles only")
            source, target = poles[segment.source], poles[segment.target]
            reach = min(factorio_entity_profile(item.prototype).copper_wire_reach or 0 for item in (source, target))
            if math.hypot(source.x - target.x, source.y - target.y) > reach + 1e-9:
                raise RealizationError("Copper wire exceeds reach")
            adjacency[segment.source].add(segment.target)
            adjacency[segment.target].add(segment.source)
        if any(len(neighbors) > 5 for neighbors in adjacency.values()):
            raise RealizationError("Pole exceeds five copper connections")
        reached: set[str] = set()
        pending = [self.external_supply]
        while pending:
            identifier = pending.pop()
            if identifier not in reached:
                reached.add(identifier)
                pending.extend(adjacency[identifier] - reached)
        if reached != poles.keys():
            raise RealizationError("Power network is disconnected from external supply")
        if any(not any(_covered(entity, pole) for pole in poles.values()) for entity in entities if entity.prototype in _POWERED):
            raise RealizationError("Powered entity lacks pole supply coverage")

    def canonical_data(self) -> dict[str, object]:
        return {
            "schema_version": 1, "kind": "factorio-finalized", "factorio_version": "2.0.77",
            "fabric": self.fabric.canonical_data(), "design": self.design.canonical_data(),
            "placement": self.placement.canonical_data(),
            "domains": [item.canonical_data() for item in sorted(self.domains, key=lambda item: item.identifier)],
            "wires": [item.canonical_data() for item in sorted(self.wires, key=lambda item: (item.domain, item.source, item.target))],
            "power_segments": [{"source": item.source, "target": item.target} for item in sorted(self.power_segments)],
            "external_supply": self.external_supply, "settling_ticks": self.settling_ticks,
            "power_layout": self.power_layout,
        }

    def get_digest(self) -> str:
        return hashlib.sha256(json.dumps(self.canonical_data(), sort_keys=True, separators=(",", ":"), allow_nan=False).encode()).hexdigest()


def _output_lamp_fabric(fabric: FactorioCircuitFabric) -> FactorioCircuitFabric:
    shared = Counter(item.read.endpoint for item in fabric.observations)
    entities = list(fabric.design.entities)
    observations = []
    domains = list(fabric.domains)
    wires = list(fabric.wires)
    for observation in fabric.observations:
        endpoint = observation.read.endpoint
        if shared[endpoint] > 1:
            lamp = RealizedEndpoint(f"lamp:{observation.name}", "circuit")
            entities.append(RealizedEntity(lamp.entity, "junction", ("circuit",), ProviderConfiguration.from_canonical_data({"native_lamp": True})))
            for index, domain in enumerate(domains):
                if endpoint in domain.endpoints and domain.color in observation.read.colors:
                    domains[index] = replace(domain, endpoints=(*domain.endpoints, lamp))
                    wires.append(FactorioDomainWire(domain.identifier, endpoint, lamp))
            observation = replace(observation, read=replace(observation.read, endpoint=lamp))
        observations.append(observation)
    endpoints = {item.name: item.read.endpoint for item in observations}
    design = replace(fabric.design, entities=tuple(entities), observations=tuple(replace(item, endpoints=(endpoints[item.name],)) for item in fabric.design.observations))
    return replace(fabric, design=design, domains=tuple(domains), wires=tuple(wires), observations=tuple(observations))


def finalize_combinational_fabric(
    fabric: FactorioCircuitFabric,
    material: MaterialDesign,
    behavior: BehaviorGraph,
    *,
    input_drivers: FactorioInputDriverMode | str = FactorioInputDriverMode.NONE,
    input_values: Mapping[str, str | int] | None = None,
    output_lamps: bool = False,
    positions: tuple[RealizedEntityPlacement, ...] | None = None,
    column_pitch: float = 8.0,
    row_pitch: float = 5.0,
    power_layout: str = "grid",
) -> FactorioFinalizedDesign:
    if not isinstance(power_layout, str) or power_layout not in {"grid", "compact"}:
        raise RealizationError("Power layout must be grid or compact")
    if any(isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value <= 0 for value in (column_pitch, row_pitch)):
        raise RealizationError("Physical layout spacing must be finite and positive")
    mode = FactorioInputDriverMode(input_drivers)
    if not isinstance(output_lamps, bool):
        raise RealizationError("Output lamps option must be boolean")
    if output_lamps:
        fabric = _output_lamp_fabric(fabric)
    analysis = fabric.validate(material, behavior)
    values = dict(input_values or {})
    if values and mode != FactorioInputDriverMode.CONSTANT:
        raise RealizationError("Input values require constant drivers")
    if values.keys() - {item.name for item in fabric.inputs}:
        raise RealizationError("Unknown physical input name")
    input_by_id = {identifier: tuple(sorted((item for item in fabric.inputs if item.endpoint.entity == identifier), key=lambda item: item.signal)) for identifier in {item.endpoint.entity for item in fabric.inputs}}
    add_by_id = {item.entity: item for item in fabric.additions}
    observation_by_id = {item.read.endpoint.entity: item for item in fabric.observations}
    entities: dict[str, FactorioEntity] = {}
    depth = {item.endpoint.entity: 0 for item in fabric.inputs}
    pending = list(fabric.additions)
    while pending:
        for addition in tuple(pending):
            dependencies = {endpoint.entity for read in (addition.first, addition.second) for endpoint in fabric.drivers(read)}
            if dependencies <= depth.keys():
                depth[addition.entity] = 1 + max((depth[item] for item in dependencies), default=0)
                pending.remove(addition)
    observation_depth = max(depth.values(), default=0) + 1
    for item in fabric.design.entities:
        depth.setdefault(item.identifier, observation_depth)
    supplied = {item.entity: item for item in positions} if positions is not None else {}
    if positions is not None:
        RealizedPlacement(fabric.design.get_digest(), positions).validate(fabric.design)
    rows: Counter[int] = Counter()
    for item in sorted(fabric.design.entities, key=lambda item: (depth[item.identifier], item.identifier)):
        configuration = ProviderConfiguration()
        if item.identifier in add_by_id:
            prototype = add_by_id[item.identifier].prototype
            configuration = _addition_configuration(add_by_id[item.identifier])
        elif item.identifier in input_by_id and mode == FactorioInputDriverMode.CONSTANT:
            prototype = "constant-combinator"
            filters = []
            for index, emission in enumerate(input_by_id[item.identifier], start=1):
                value = parse_factorio_input_value(values.get(emission.name, 0))
                if value >= (1 << emission.width):
                    raise RealizationError("Input driver value exceeds declared input width")
                count = value if value < (1 << 31) else value - (1 << 32)
                filters.append(FactorioConstantFilter(FactorioBlueprintSignal(emission.signal), count, index))
            configuration = ProviderConfiguration.from_canonical_data(FactorioConstantControlBehavior(tuple(filters)).canonical_data())
        elif item.identifier in observation_by_id and (output_lamps or item.configuration.canonical_data().get("native_lamp", False)):
            prototype = "small-lamp"
            configuration = ProviderConfiguration.from_canonical_data(FactorioLampControlBehavior(
                FactorioBlueprintSignal(observation_by_id[item.identifier].read.signal)
            ).canonical_data())
        else:
            prototype = "medium-electric-pole"
        position = supplied.get(item.identifier)
        if position is None:
            column = depth[item.identifier]
            x, y = _snap_entity_position(prototype, column * column_pitch, rows[column] * row_pitch, 0.0)
            rows[column] += 1
            position = RealizedEntityPlacement(item.identifier, x, y)
        entities[item.identifier] = FactorioEntity(item.identifier, prototype, position.x, position.y, position.angle, configuration=configuration)
    for index, entity in enumerate(tuple(entities.values())):
        if entity.angle not in {0.0, 90.0, 180.0, 270.0} or _snap_entity_position(entity.prototype, entity.x, entity.y, entity.angle) != (entity.x, entity.y):
            raise RealizationInfeasibleError("Placement must use tile-aligned quarter turns")
        if any(entities_collide(entity, other) for other in tuple(entities.values())[index + 1:]):
            raise RealizationInfeasibleError("Initial physical entities overlap")
    origin = _place_supply(entities, power_layout)
    if origin is not None:
        _grid_power(entities, origin)
    def physical(endpoint):
        return physical_connector(endpoint, entities[endpoint.entity])

    def logical(endpoint):
        entity = entities[endpoint.entity]
        if entity.prototype in {"arithmetic-combinator", "decider-combinator"}:
            port = "in" if endpoint.connector == 1 else "out"
        else:
            port = "out" if endpoint.entity in input_by_id else "circuit"
        return RealizedEndpoint(endpoint.entity, port)

    def link(source, target, route_id):
        try:
            relays, links = route_connector_link(source, target, entities, route_id)
        except FactorioRoutingError as error:
            raise RealizationInfeasibleError(str(error)) from error
        if any(relay.identifier in entities for relay in relays):
            raise RealizationError("Generated relay identifier collision")
        entities.update((item.identifier, item) for item in relays)
        return relays, links

    domains: list[FactorioCircuitDomain] = []
    wires: list[FactorioDomainWire] = []
    owners = {(physical(endpoint), domain.color): domain.identifier for domain in fabric.domains for endpoint in domain.endpoints}
    logical_entities = {item.identifier for item in fabric.design.entities}
    for domain_index, domain in enumerate(sorted(fabric.domains, key=lambda item: item.identifier)):
        connected = {physical(min(domain.endpoints))}
        remaining = {physical(item) for item in domain.endpoints} - connected
        route_index = 0
        while remaining:
            source, target = min(
                ((source, target) for source in connected for target in remaining),
                key=lambda pair: (connector_distance(entities[pair[0].entity], pair[0].connector, entities[pair[1].entity], pair[1].connector), pair),
            )
            available = {
                item.identifier for item in entities.values()
                if item.prototype in _POLES
                and (item.identifier not in logical_entities or logical(FactorioConnectorEndpoint(item.identifier, 1)) in domain.endpoints)
                and owners.get((FactorioConnectorEndpoint(item.identifier, 1), domain.color), domain.identifier) == domain.identifier
            }
            links = _existing_pole_route(source, target, entities, available)
            if links is None:
                _, links = link(source, target, f"domain:{domain_index}:{route_index}")
            route_index += 1
            wires.extend(FactorioDomainWire(domain.identifier, logical(left), logical(right)) for left, right in links)
            for left, right in links:
                connected.update((left, right))
                owners[left, domain.color] = domain.identifier
                owners[right, domain.color] = domain.identifier
            connected.add(target)
            remaining -= connected
        domains.append(replace(domain, endpoints=tuple(sorted(logical(item) for item in connected))))
    used = {endpoint.entity for domain in domains for endpoint in domain.endpoints}
    for pole in tuple(entities.values()):
        if not pole.identifier.startswith("power:supply:") or pole.identifier in used:
            continue
        others = [item for item in entities.values() if item.prototype in _POLES and item.identifier != pole.identifier]
        if all(any(_covered(item, other) for other in others) for item in entities.values() if item.prototype in _POWERED):
            del entities[pole.identifier]
    if not any(item.prototype in _POLES for item in entities.values()):
        left = min((entity_bounds(item)[0] for item in entities.values()), default=0)
        entities["power:external"] = FactorioEntity("power:external", "medium-electric-pole", math.floor(left) - 2.5, 0.5)
    poles = {item.identifier for item in entities.values() if item.prototype in _POLES}
    external_supply = min(poles)
    connected_poles = {external_supply}
    remaining_poles = poles - connected_poles if origin is None else set()
    power: list[FactorioPowerSegment] = list(_grid_power(entities, origin)) if origin is not None else []
    degrees: Counter[str] = Counter()
    while remaining_poles:
        source, target = min(
            ((source, target) for source in connected_poles if degrees[source] < 5 for target in remaining_poles),
            key=lambda pair: (math.hypot(entities[pair[0]].x - entities[pair[1]].x, entities[pair[0]].y - entities[pair[1]].y), pair),
        )
        relays, links = link(FactorioConnectorEndpoint(source, 1), FactorioConnectorEndpoint(target, 1), f"power:{len(power)}")
        for left, right in links:
            power.append(FactorioPowerSegment(left.entity, right.entity))
            degrees[left.entity] += 1
            degrees[right.entity] += 1
        connected_poles.update(item.identifier for item in relays)
        connected_poles.add(target)
        remaining_poles.remove(target)
    original = {item.identifier: item for item in fabric.design.entities}
    inventory = tuple(RealizedEntity(
        item.identifier, item.prototype,
        original[item.identifier].ports if item.identifier in original else ("circuit",),
        item.configuration,
    ) for item in sorted(entities.values(), key=lambda item: item.identifier))
    design = replace(fabric.design, entities=inventory, provenance=(*fabric.design.provenance, RealizationProvenance(
        "factorio/physical-add32/v1", tuple(item.identifier for item in inventory),
    )))
    placement = RealizedPlacement(design.get_digest(), tuple(RealizedEntityPlacement(
        item.identifier, item.x, item.y, item.angle,
    ) for item in entities.values()))
    result = FactorioFinalizedDesign(fabric, design, placement, tuple(domains), tuple(wires), tuple(power), external_supply, analysis.settling_ticks, power_layout)
    result.validate(material, behavior)
    return result


finalize_add_fabric = finalize_combinational_fabric