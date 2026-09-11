from __future__ import annotations

from collections.abc import Mapping, Sequence
import hashlib
import json
import math

from gateforge.graph import MaterialGraph, ModuleValueSubject
from gateforge.material import (
    MaterialDesign,
    MaterialModuleValueRef,
    MaterialNetId,
    MaterialObjectPortRef,
)
from gateforge.placement import (
    ComponentPlacement,
    PhysicalEndpoint,
    PhysicalEndpointKind,
    PlacedDesign,
)
from gateforge.providers.factorio.common import FACTORIO_LAMP, FACTORIO_PROVIDER
from gateforge.providers.factorio.configuration import (
    encode_factorio_lamp_configuration,
)
from gateforge.providers.factorio.profile import factorio_entity_profile
from gateforge.providers.factorio.routed import (
    FactorioConnectorEndpoint,
    FactorioEntity,
    FactorioInputDriver,
    FactorioInputDriverMode,
    FactorioOutputLamp,
    FactorioPowerSegment,
    FactorioRoutedDesign,
    FactorioRoutingError,
    FactorioSignalAssignment,
    FactorioWireColor,
    FactorioWireSegment,
    connector_distance,
    connector_position,
    entities_collide,
)
from gateforge.target import PortDirection, ProviderConfiguration


_PORT_SIGNALS = {
    "a": ("signal-A", FactorioWireColor.RED),
    "b": ("signal-B", FactorioWireColor.GREEN),
    "y": ("signal-C", FactorioWireColor.RED),
}
_POLES = {"medium-electric-pole", "big-electric-pole"}


def parse_factorio_input_value(value: str | int) -> int:
    if isinstance(value, bool):
        raise FactorioRoutingError("Factorio input values must be integers")
    if isinstance(value, int):
        parsed = value
    elif isinstance(value, str):
        try:
            parsed = int(value, 0)
        except ValueError as error:
            raise FactorioRoutingError(
                f"Invalid Factorio input value {value!r}"
            ) from error
    else:
        raise FactorioRoutingError("Factorio input values must be integers")
    return parsed % (1 << 32)


def placement_digest(placement: PlacedDesign) -> str:
    encoded = json.dumps(
        placement.canonical_data(),
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def build_factorio_routed_design(
    material: MaterialDesign,
    graph: MaterialGraph,
    placement: PlacedDesign,
    *,
    input_drivers: FactorioInputDriverMode | str = FactorioInputDriverMode.NONE,
    input_values: Mapping[str, str | int] | None = None,
    output_lamps: bool = False,
) -> FactorioRoutedDesign:
    if graph.design != material:
        raise FactorioRoutingError(
            "Factorio routing graph does not contain the supplied material"
        )
    if placement.material_digest != material.get_digest():
        raise FactorioRoutingError(
            "Factorio placement does not match the supplied material"
        )
    if placement.target != FACTORIO_PROVIDER:
        raise FactorioRoutingError(
            f"Cannot route target {placement.target!r} as Factorio"
        )
    if len(placement.containers) != 1:
        raise FactorioRoutingError("Factorio routing currently requires flat placement")
    if not isinstance(output_lamps, bool):
        raise FactorioRoutingError("Factorio output_lamps must be boolean")
    try:
        driver_mode = FactorioInputDriverMode(input_drivers)
    except ValueError as error:
        raise FactorioRoutingError(
            f"Unknown Factorio input driver mode {input_drivers!r}"
        ) from error

    assignments = _signal_assignments(material)
    assignment_by_net = {item.net: item for item in assignments}
    entities = {
        component.identifier: _placed_entity(component)
        for component in placement.components
    }
    subject_entities = {
        component.source: component.identifier
        for component in placement.components
    }
    values = dict(input_values or {})
    if driver_mode == FactorioInputDriverMode.NONE and values:
        raise FactorioRoutingError(
            "Factorio input values require constant input drivers"
        )

    input_ports = _input_module_values(material)
    unknown_values = set(values) - set(input_ports)
    if unknown_values:
        raise FactorioRoutingError(
            f"Unknown Factorio input ports {sorted(unknown_values)!r}"
        )
    drivers: list[FactorioInputDriver] = []
    pending_links: list[
        tuple[MaterialNetId, FactorioConnectorEndpoint, FactorioConnectorEndpoint]
    ] = []
    if driver_mode == FactorioInputDriverMode.CONSTANT:
        for port_name, (net, attachment) in sorted(input_ports.items()):
            subject = ModuleValueSubject(
                attachment.module,
                attachment.port,
                attachment.bits,
                attachment.direction,
            )
            try:
                terminal_id = subject_entities[subject]
            except KeyError as error:
                raise FactorioRoutingError(
                    f"Factorio input {port_name!r} has no terminal entity"
                ) from error
            unsigned_value = parse_factorio_input_value(values.get(port_name, 0))
            signed_count = (
                unsigned_value
                if unsigned_value < (1 << 31)
                else unsigned_value - (1 << 32)
            )
            assignment = assignment_by_net[net]
            driver = _input_driver_entity(
                port_name,
                assignment.signal,
                signed_count,
                entities[terminal_id],
                tuple(entities.values()),
            )
            entities[driver.identifier] = driver
            drivers.append(
                FactorioInputDriver(
                    port_name,
                    net,
                    driver.identifier,
                    unsigned_value,
                    signed_count,
                )
            )
            pending_links.append(
                (
                    net,
                    FactorioConnectorEndpoint(driver.identifier, 1),
                    FactorioConnectorEndpoint(terminal_id, 1),
                )
            )

    lamps: list[FactorioOutputLamp] = []
    if output_lamps:
        for port_name, (net, attachment) in sorted(
            _output_module_values(material).items()
        ):
            subject = ModuleValueSubject(
                attachment.module,
                attachment.port,
                attachment.bits,
                attachment.direction,
            )
            try:
                terminal_id = subject_entities[subject]
            except KeyError as error:
                raise FactorioRoutingError(
                    f"Factorio output {port_name!r} has no terminal entity"
                ) from error
            assignment = assignment_by_net[net]
            lamp = _output_lamp_entity(
                port_name,
                entities[terminal_id],
                tuple(entities.values()),
            )
            entities[lamp.identifier] = lamp
            lamps.append(
                FactorioOutputLamp(
                    port_name,
                    net,
                    lamp.identifier,
                    terminal_id,
                    assignment.signal,
                    assignment.color,
                )
            )
            pending_links.append(
                (
                    net,
                    FactorioConnectorEndpoint(terminal_id, 1),
                    FactorioConnectorEndpoint(lamp.identifier, 1),
                )
            )

    for connection in placement.root_container.connections:
        pending_links.append(
            (
                connection.net,
                _placed_endpoint(connection.source, entities),
                _placed_endpoint(connection.target, entities),
            )
        )

    wires: list[FactorioWireSegment] = []
    for route_index, (net, source, target) in enumerate(
        sorted(
            pending_links,
            key=lambda item: (
                item[0].value,
                item[1].entity,
                item[1].connector,
                item[2].entity,
                item[2].connector,
            ),
        )
    ):
        assignment = assignment_by_net[net]
        route_entities, route_wires = _route_link(
            net,
            assignment.color,
            source,
            target,
            entities,
            route_index,
        )
        entities.update(
            (entity.identifier, entity) for entity in route_entities
        )
        wires.extend(route_wires)

    return FactorioRoutedDesign(
        material_digest=material.get_digest(),
        placement_digest=placement_digest(placement),
        entities=tuple(entities.values()),
        signal_assignments=assignments,
        wires=tuple(wires),
        power_segments=_power_segments(entities, wires),
        input_drivers=tuple(drivers),
        output_lamps=tuple(lamps),
    )


def _signal_assignments(
    material: MaterialDesign,
) -> tuple[FactorioSignalAssignment, ...]:
    assignments = []
    used_signals = set()
    objects = {item.identifier: item for item in material.objects}
    for net in material.nets:
        ports = {
            attachment.port
            for attachment in net.attachments
            if isinstance(attachment, MaterialObjectPortRef)
            and objects[attachment.object].type != FACTORIO_LAMP
        }
        if len(ports) != 1:
            raise FactorioRoutingError(
                f"Factorio smoke routing requires one arithmetic role per net; "
                f"net {net.identifier.value} has {sorted(ports)!r}"
            )
        port = next(iter(ports))
        try:
            signal, color = _PORT_SIGNALS[port]
        except KeyError as error:
            raise FactorioRoutingError(
                f"Unsupported Factorio arithmetic port {port!r}"
            ) from error
        if signal in used_signals:
            raise FactorioRoutingError(
                f"Factorio signal {signal!r} is assigned to multiple logical nets"
            )
        used_signals.add(signal)
        assignments.append(FactorioSignalAssignment(net.identifier, signal, color))
    return tuple(sorted(assignments, key=lambda item: item.net.value))


def _input_module_values(
    material: MaterialDesign,
) -> dict[str, tuple[MaterialNetId, MaterialModuleValueRef]]:
    result = {}
    for net in material.nets:
        for attachment in net.attachments:
            if (
                isinstance(attachment, MaterialModuleValueRef)
                and attachment.direction == PortDirection.INPUT
            ):
                if attachment.port in result:
                    raise FactorioRoutingError(
                        f"Duplicate Factorio input port {attachment.port!r}"
                    )
                result[attachment.port] = (net.identifier, attachment)
    return result


def _output_module_values(
    material: MaterialDesign,
) -> dict[str, tuple[MaterialNetId, MaterialModuleValueRef]]:
    result = {}
    for net in material.nets:
        for attachment in net.attachments:
            if (
                isinstance(attachment, MaterialModuleValueRef)
                and attachment.direction == PortDirection.OUTPUT
            ):
                if attachment.port in result:
                    raise FactorioRoutingError(
                        f"Duplicate Factorio output port {attachment.port!r}"
                    )
                result[attachment.port] = (net.identifier, attachment)
    return result


def _placed_entity(component: ComponentPlacement) -> FactorioEntity:
    x, y = _snap_entity_position(
        component.kind,
        component.x,
        component.y,
        component.angle,
    )
    return FactorioEntity(
        identifier=component.identifier,
        prototype=component.kind,
        x=x,
        y=y,
        angle=component.angle,
        source_component=component.identifier,
        configuration=component.payload,
    )


def _placed_endpoint(
    endpoint: PhysicalEndpoint,
    entities: Mapping[str, FactorioEntity],
) -> FactorioConnectorEndpoint:
    if endpoint.kind != PhysicalEndpointKind.COMPONENT:
        raise FactorioRoutingError(
            "Factorio routing currently supports component endpoints only"
        )
    try:
        entity = entities[endpoint.identifier]
    except KeyError as error:
        raise FactorioRoutingError(
            f"Factorio connection references unknown entity {endpoint.identifier!r}"
        ) from error
    if entity.prototype == "arithmetic-combinator":
        if endpoint.port in {0, 1}:
            connector = 1
        elif endpoint.port == 2:
            connector = 2
        else:
            raise FactorioRoutingError(
                f"Unknown arithmetic connector role {endpoint.port}"
            )
    else:
        if endpoint.port != 0:
            raise FactorioRoutingError(
                f"Unknown {entity.prototype} connector role {endpoint.port}"
            )
        connector = 1
    return FactorioConnectorEndpoint(endpoint.identifier, connector)


def _input_driver_entity(
    port: str,
    signal: str,
    signed_count: int,
    terminal: FactorioEntity,
    occupied: Sequence[FactorioEntity],
) -> FactorioEntity:
    identifier = f"factorio:input-driver:{port}"
    offsets = (
        (-2.0, 0.0),
        (0.0, -2.0),
        (0.0, 2.0),
        (2.0, 0.0),
        (-3.0, 0.0),
        (0.0, -3.0),
        (0.0, 3.0),
        (3.0, 0.0),
    )
    for offset_x, offset_y in offsets:
        x, y = _snap_entity_position(
            "constant-combinator",
            terminal.x + offset_x,
            terminal.y + offset_y,
            0.0,
        )
        candidate = FactorioEntity(
            identifier,
            "constant-combinator",
            x,
            y,
            configuration=ProviderConfiguration.from_canonical_data(
                {"signal": signal, "count": signed_count}
            ),
        )
        if not any(entities_collide(candidate, entity) for entity in occupied):
            return candidate
    raise FactorioRoutingError(
        f"Cannot place constant input driver for port {port!r}"
    )


def _output_lamp_entity(
    port: str,
    terminal: FactorioEntity,
    occupied: Sequence[FactorioEntity],
) -> FactorioEntity:
    identifier = f"factorio:output-lamp:{port}"
    offsets = (
        (1.0, 0.0),
        (0.0, -1.0),
        (0.0, 1.0),
        (-1.0, 0.0),
        (2.0, 0.0),
        (0.0, -2.0),
        (0.0, 2.0),
        (-2.0, 0.0),
    )
    for offset_x, offset_y in offsets:
        x, y = _snap_entity_position(
            "small-lamp",
            terminal.x + offset_x,
            terminal.y + offset_y,
            0.0,
        )
        candidate = FactorioEntity(
            identifier,
            "small-lamp",
            x,
            y,
            configuration=encode_factorio_lamp_configuration(),
        )
        if not any(entities_collide(candidate, entity) for entity in occupied):
            return candidate
    raise FactorioRoutingError(f"Cannot place output lamp for port {port!r}")


def _route_link(
    net: MaterialNetId,
    color: FactorioWireColor,
    source: FactorioConnectorEndpoint,
    target: FactorioConnectorEndpoint,
    entities: Mapping[str, FactorioEntity],
    route_index: int,
) -> tuple[tuple[FactorioEntity, ...], tuple[FactorioWireSegment, ...]]:
    relays, links = route_connector_link(source, target, entities, f"{net.value}:{route_index}")
    return relays, tuple(FactorioWireSegment(net, color, left, right) for left, right in links)


def route_connector_link(
    source: FactorioConnectorEndpoint,
    target: FactorioConnectorEndpoint,
    entities: Mapping[str, FactorioEntity],
    route_id: str,
) -> tuple[tuple[FactorioEntity, ...], tuple[tuple[FactorioConnectorEndpoint, FactorioConnectorEndpoint], ...]]:
    source_entity = entities[source.entity]
    target_entity = entities[target.entity]
    if _within_reach(source_entity, source.connector, target_entity, target.connector):
        return (), ((source, target),)

    source_position = connector_position(source_entity, source.connector)
    target_position = connector_position(target_entity, target.connector)
    distance = math.dist(source_position, target_position)
    if distance <= 0:
        raise FactorioRoutingError(f"Cannot route coincident endpoints on {route_id}")
    unit_x = (target_position[0] - source_position[0]) / distance
    unit_y = (target_position[1] - source_position[1]) / distance

    if distance <= 18.0:
        plan = [("medium-electric-pole", distance / 2)]
    else:
        start_distance = 0.0
        end_distance = distance
        start_prototype = source_entity.prototype
        end_prototype = target_entity.prototype
        plan: list[tuple[str, float]] = []
        if start_prototype not in _POLES:
            start_distance = 7.0
            plan.append(("medium-electric-pole", start_distance))
            start_prototype = "medium-electric-pole"
        if end_prototype not in _POLES:
            end_distance = distance - 7.0
            end_medium = ("medium-electric-pole", end_distance)
            end_prototype = "medium-electric-pole"
        else:
            end_medium = None

        trunk_length = end_distance - start_distance
        start_reach = (
            factorio_entity_profile(start_prototype).circuit_wire_reach - 1.0
        )
        end_reach = (
            factorio_entity_profile(end_prototype).circuit_wire_reach - 1.0
        )
        if trunk_length > min(start_reach, end_reach):
            first_offset = min(start_reach, trunk_length / 2)
            last_offset = max(trunk_length - end_reach, trunk_length / 2)
            if first_offset == last_offset:
                big_offsets = (first_offset,)
            else:
                big_pole_count = math.ceil(
                    (last_offset - first_offset) / 31.0
                ) + 1
                step = (last_offset - first_offset) / (big_pole_count - 1)
                big_offsets = tuple(
                    first_offset + index * step
                    for index in range(big_pole_count)
                )
            plan.extend(
                ("big-electric-pole", start_distance + offset)
                for offset in big_offsets
            )
        if end_medium is not None:
            plan.append(end_medium)

    planned = tuple(sorted(plan, key=lambda item: item[1]))
    perpendicular_x = -unit_y
    perpendicular_y = unit_x
    for lateral in (0.0, 1.0, -1.0, 2.0, -2.0, 3.0, -3.0, 4.0, -4.0):
        relays = []
        for relay_index, (prototype, offset) in enumerate(planned):
            x, y = _snap_entity_position(
                prototype,
                source_position[0] + unit_x * offset + perpendicular_x * lateral,
                source_position[1] + unit_y * offset + perpendicular_y * lateral,
                0.0,
            )
            relays.append(
                FactorioEntity(
                    identifier=(
                        f"factorio:relay:{route_id}:{relay_index}"
                    ),
                    prototype=prototype,
                    x=x,
                    y=y,
                )
            )
        relay_tuple = tuple(relays)
        if _route_entities_collide(relay_tuple, tuple(entities.values())):
            continue
        endpoints = (
            source,
            *(
                FactorioConnectorEndpoint(entity.identifier, 1)
                for entity in relay_tuple
            ),
            target,
        )
        route_entities = {
            **entities,
            **{item.identifier: item for item in relay_tuple},
        }
        if all(
            _within_reach(
                route_entities[left.entity],
                left.connector,
                route_entities[right.entity],
                right.connector,
            )
            for left, right in zip(endpoints, endpoints[1:])
        ):
            return relay_tuple, tuple(zip(endpoints, endpoints[1:]))

    limiting_reach = min(
        factorio_entity_profile(source_entity.prototype).circuit_wire_reach,
        factorio_entity_profile(target_entity.prototype).circuit_wire_reach,
    )
    raise FactorioRoutingError(
        f"Cannot route {route_id} between {source.entity!r} and "
        f"{target.entity!r}; distance={distance:.3f}, endpoint reach="
        f"{limiting_reach:.3f}"
    )


def _power_segments(
    entities: Mapping[str, FactorioEntity],
    wires: Sequence[FactorioWireSegment],
) -> tuple[FactorioPowerSegment, ...]:
    return tuple(
        sorted(
            {
                FactorioPowerSegment(wire.source.entity, wire.target.entity)
                for wire in wires
                if entities[wire.source.entity].prototype in _POLES
                and entities[wire.target.entity].prototype in _POLES
            }
        )
    )


def _snap_entity_position(
    prototype: str,
    x: float,
    y: float,
    angle: float,
) -> tuple[float, float]:
    profile = factorio_entity_profile(prototype)
    quarter_turn = round(angle / 90.0) % 2
    width = profile.height if quarter_turn else profile.width
    height = profile.width if quarter_turn else profile.height
    return _snap_axis(x, width), _snap_axis(y, height)


def _snap_axis(value: float, extent: float) -> float:
    offset = 0.5 if round(extent) % 2 else 0.0
    return math.floor(value - offset + 0.5) + offset


def _within_reach(
    left: FactorioEntity,
    left_connector: int,
    right: FactorioEntity,
    right_connector: int,
) -> bool:
    reach = min(
        factorio_entity_profile(left.prototype).circuit_wire_reach,
        factorio_entity_profile(right.prototype).circuit_wire_reach,
    )
    return connector_distance(left, left_connector, right, right_connector) <= reach + 1e-9


def _route_entities_collide(
    relays: Sequence[FactorioEntity],
    occupied: Sequence[FactorioEntity],
) -> bool:
    for index, relay in enumerate(relays):
        if any(entities_collide(relay, entity) for entity in occupied):
            return True
        if any(entities_collide(relay, other) for other in relays[:index]):
            return True
    return False