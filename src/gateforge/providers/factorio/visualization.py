from __future__ import annotations

from collections.abc import Mapping
from dataclasses import replace

from gateforge.providers.factorio.finalization import FactorioFinalizedDesign, physical_connector

from gateforge.graph import ModuleValueSubject, ObjectSubject
from gateforge.material import MaterialObject
from gateforge.providers.factorio.common import (
    FACTORIO_ARITHMETIC_COMBINATOR,
    FACTORIO_LAMP,
)
from gateforge.providers.factorio.configuration import (
    FactorioArithmeticConfiguration,
    FactorioLampConfiguration,
    FactorioObjectConfigurationCodec,
)
from gateforge.providers.factorio.profile import factorio_entity_profile
from gateforge.providers.factorio.routed import (
    FactorioEntity,
    FactorioRoutedDesign,
    FactorioSignalAssignment,
    FactorioWireColor,
    connector_distance,
    connector_position,
    entity_bounds,
)
from gateforge.providers.factorio.routing import build_factorio_routed_design
from gateforge.target import PortDirection, TargetTypeRegistry
from gateforge.visualization.model import (
    VisualBounds,
    VisualElement,
    VisualElementDescriptor,
    VisualEndpoint,
    VisualEllipse,
    VisualPoint,
    VisualNet,
    VisualPolyline,
    VisualPrimitive,
    VisualPort,
    VisualProperty,
    VisualRectangle,
    VisualReference,
    VisualScene,
    VisualStyle,
    VisualText,
    VisualTransform,
    VisualView,
)
from gateforge.visualization.provider import VisualizationRequest


_WIRE_COLORS = {
    FactorioWireColor.RED: "#d64b45",
    FactorioWireColor.GREEN: "#3f9b55",
}
_COPPER_WIRE_COLOR = "#b87333"
_TILE_SIZE = 32.0


def build_finalized_factorio_view(finalized: FactorioFinalizedDesign) -> VisualView:
    finalized.validate_physical()
    entities = finalized.entities
    by_id = {item.identifier: item for item in entities}
    elements: list[VisualElement] = []
    inputs = {identifier: ", ".join(sorted(item.name for item in finalized.fabric.inputs if item.endpoint.entity == identifier)) for identifier in {item.endpoint.entity for item in finalized.fabric.inputs}}
    observations = {identifier: ", ".join(sorted(item.name for item in finalized.fabric.observations if item.read.endpoint.entity == identifier)) for identifier in {item.read.endpoint.entity for item in finalized.fabric.observations}}
    logical = {item.identifier: item for item in finalized.fabric.design.entities}
    for entity in entities:
        profile = factorio_entity_profile(entity.prototype)
        bounds = VisualBounds.centered(profile.width * _TILE_SIZE, profile.height * _TILE_SIZE)
        color, label = {
            "arithmetic-combinator": ("#c9d2d4", "+"),
            "decider-combinator": ("#c6d9b4", "="),
            "constant-combinator": ("#f2c14e", "="),
            "small-lamp": ("#f4dc72", ">0"),
            "medium-electric-pole": ("#93a3a7", "P"),
            "big-electric-pole": ("#6f7f83", "P"),
        }[entity.prototype]
        if entity.prototype == "arithmetic-combinator":
            label = entity.configuration.canonical_data()["arithmetic_conditions"]["operation"]
        elif entity.prototype == "decider-combinator":
            label = entity.configuration.canonical_data()["decider_conditions"]["conditions"][0]["comparator"]
        shape = VisualRectangle if entity.prototype in {"arithmetic-combinator", "decider-combinator", "constant-combinator"} else VisualEllipse
        primitives: tuple[VisualPrimitive, ...] = (shape(bounds, VisualStyle(fill=color, stroke="#26383b", stroke_width=2.0)),)
        if label:
            primitives += (VisualText(VisualPoint(0, -bounds.height * 0.3 if entity.prototype.endswith("electric-pole") else 0), label),)
        original = logical.get(entity.identifier)
        metadata = original.configuration.canonical_data() if original is not None else {}
        role = (
            f"Input {inputs[entity.identifier]} ({'constant driver' if entity.prototype == 'constant-combinator' else 'external pole'})" if entity.identifier in inputs
            else f"Output {observations[entity.identifier]}" if entity.identifier in observations
            else f"Output bus {metadata['interface_circuit']}" if metadata.get("interface_direction") == "output"
            else "Arithmetic" if entity.prototype == "arithmetic-combinator"
            else "Decider" if entity.prototype == "decider-combinator"
            else "Power supply" if entity.identifier.startswith("power:") else "Circuit relay"
        )
        properties = (
            VisualProperty("Prototype", entity.prototype, "Entity"),
            VisualProperty("Role", role, "Entity"),
            VisualProperty("Tile X", f"{entity.x:g}", "Geometry"),
            VisualProperty("Tile Y", f"{entity.y:g}", "Geometry"),
            VisualProperty("ID", entity.identifier, "Identity"),
            VisualProperty("Configuration", entity.configuration.canonical_json, "Entity"),
            VisualProperty("Settling ticks", str(finalized.settling_ticks), "Circuit"),
            VisualProperty("External supply", str(entity.identifier == finalized.external_supply), "Power"),
        )
        if entity.prototype == "arithmetic-combinator":
            properties += (VisualProperty("Operation", label, "Arithmetic"),)
        elif entity.prototype == "decider-combinator":
            properties += (VisualProperty("Comparator", label, "Condition"), VisualProperty("True output", "1", "Condition"))
        if original is not None:
            properties += tuple(VisualProperty(name.removeprefix("interface_").title(), str(value), "Interface") for name, value in metadata.items() if name.startswith("interface_") and name != "interface_bindings")
            properties += tuple(VisualProperty(f"Port {name}", signal, "Interface") for name, signal in metadata.get("interface_bindings", {}).items())
        for emission in finalized.fabric.inputs:
            if emission.endpoint.entity == entity.identifier and "interface_bindings" not in metadata:
                properties += (VisualProperty("Signal", emission.signal, "Interface"),)
        for observation in finalized.fabric.observations:
            if observation.read.endpoint.entity == entity.identifier and "interface_bindings" not in metadata:
                properties += (VisualProperty("Signal", observation.read.signal, "Interface"),)
        references = {VisualReference("realized_entity", entity.identifier)}
        for origin in finalized.design.provenance:
            if entity.identifier in origin.entities:
                references.update(VisualReference("material_object", item.value) for item in origin.material_objects)
                references.update(VisualReference("behavior_value", value) for value in origin.values)
        ports = tuple(VisualPort(
            f"connector:{connector.identifier}", str(connector.identifier),
            PortDirection.INOUT,
            VisualPoint(connector.x * _TILE_SIZE, connector.y * _TILE_SIZE),
        ) for connector in profile.connectors)
        elements.append(VisualElement(
            f"factorio-entity:{entity.identifier}",
            VisualElementDescriptor(entity.prototype, bounds, primitives, ports, properties),
            VisualTransform(entity.x * _TILE_SIZE, entity.y * _TILE_SIZE, entity.angle),
            tuple(sorted(references)), layer=1,
        ))
    domains = {item.identifier: item for item in finalized.domains}
    channels = finalized.fabric.analyze().channels
    for index, wire in enumerate(finalized.wires):
        source = physical_connector(wire.source, by_id[wire.source.entity])
        target = physical_connector(wire.target, by_id[wire.target.entity])
        keys = ", ".join(item.signal for item in channels if item.domain == wire.domain)
        element = _wire_element(
            index, wire.domain, keys, domains[wire.domain].color,
            connector_position(by_id[source.entity], source.connector),
            connector_position(by_id[target.entity], target.connector), show_label=False,
        )
        elements.append(replace(
            element,
            references=(VisualReference("factorio_domain", wire.domain),),
            descriptor=replace(element.descriptor, properties=(
                VisualProperty("Domain", wire.domain, "Circuit"),
                VisualProperty("Signal keys", keys, "Circuit"),
                VisualProperty("Color", domains[wire.domain].color.value, "Circuit"),
            )),
        ))
    for index, segment in enumerate(finalized.power_segments):
        source, target = by_id[segment.source], by_id[segment.target]
        wire = _power_wire_element(index, segment.source, segment.target, (source.x, source.y), (target.x, target.y))
        elements.append(replace(wire, descriptor=replace(wire.descriptor, properties=(*wire.descriptor.properties,
            VisualProperty("Source prototype", source.prototype, "Power"),
            VisualProperty("Target prototype", target.prototype, "Power"),
        ))))
    nets = tuple(VisualNet(
        domain.identifier, domain.identifier,
        tuple(VisualEndpoint(
            f"factorio-entity:{endpoint.entity}",
            f"connector:{physical_connector(endpoint, by_id[endpoint.entity]).connector}",
        ) for endpoint in domain.endpoints),
        (VisualProperty("Color", domain.color.value, "Circuit"),),
    ) for domain in finalized.domains if len(domain.endpoints) > 1)
    scene = VisualScene("factorio:finalized", "Finalized Factorio circuit", _scene_bounds(entities), tuple(elements), nets, grid_size=_TILE_SIZE)
    return VisualView("factorio:finalized", "Factorio finalized", (scene,))


class FactorioVisualizationAdapter:
    def describe_material_object(
        self,
        material_object: MaterialObject,
        registry: TargetTypeRegistry,
    ) -> VisualElementDescriptor:
        _ = registry
        configuration = FactorioObjectConfigurationCodec().decode(
            material_object.type,
            material_object.configuration,
        )
        if isinstance(configuration, FactorioArithmeticConfiguration):
            return _logical_arithmetic_descriptor(configuration)
        if isinstance(configuration, FactorioLampConfiguration):
            return _logical_lamp_descriptor(configuration)
        raise ValueError(f"Unsupported Factorio object type {material_object.type}")

    def build_realized_views(
        self,
        request: VisualizationRequest,
    ) -> tuple[VisualView, ...]:
        values = request.options.get("input_values", {})
        if not isinstance(values, Mapping):
            raise ValueError("Factorio visualization input_values must be an object")
        input_drivers = request.options.get("input_drivers", "none")
        if not isinstance(input_drivers, str):
            raise ValueError("Factorio visualization input_drivers must be a string")
        output_lamps = request.options.get("output_lamps", False)
        if not isinstance(output_lamps, bool):
            raise ValueError("Factorio visualization output_lamps must be boolean")
        routed = build_factorio_routed_design(
            request.material,
            request.graph,
            request.placement,
            input_drivers=input_drivers,
            input_values=values,
            output_lamps=output_lamps,
        )
        return (
            _factorio_view(request, routed, routed_view=False),
            _factorio_view(request, routed, routed_view=True),
        )


def _factorio_view(
    request: VisualizationRequest,
    routed: FactorioRoutedDesign,
    *,
    routed_view: bool,
) -> VisualView:
    entities = (
        routed.entities
        if routed_view
        else tuple(
            item
            for item in routed.entities
            if not item.identifier.startswith("factorio:relay:")
        )
    )
    entity_by_id = {item.identifier: item for item in routed.entities}
    component_by_id = {item.identifier: item for item in request.placement.components}
    elements = [
        _entity_element(
            entity,
            component_by_id,
            routed,
        )
        for entity in entities
    ]
    if routed_view:
        assignment_by_net = {
            item.net: item for item in routed.signal_assignments
        }
        label_segments = {
            max(
                (
                    (index, wire)
                    for index, wire in enumerate(routed.wires)
                    if wire.net == assignment.net
                ),
                key=lambda item: (
                    connector_distance(
                        entity_by_id[item[1].source.entity],
                        item[1].source.connector,
                        entity_by_id[item[1].target.entity],
                        item[1].target.connector,
                    ),
                    -item[0],
                ),
            )[0]
            for assignment in routed.signal_assignments
        }
        for index, wire in enumerate(routed.wires):
            assignment = assignment_by_net[wire.net]
            source_position = connector_position(
                entity_by_id[wire.source.entity],
                wire.source.connector,
            )
            target_position = connector_position(
                entity_by_id[wire.target.entity],
                wire.target.connector,
            )
            elements.append(
                _wire_element(
                    index,
                    wire.net.value,
                    assignment.signal,
                    wire.color,
                    source_position,
                    target_position,
                    show_label=index in label_segments,
                )
            )
        for index, segment in enumerate(routed.power_segments):
            source = entity_by_id[segment.source]
            target = entity_by_id[segment.target]
            elements.append(
                _power_wire_element(
                    index,
                    segment.source,
                    segment.target,
                    (source.x, source.y),
                    (target.x, target.y),
                )
            )

    scene = VisualScene(
        identifier=request.placement.root,
        label=request.placement.root_container.name,
        bounds=_scene_bounds(entities),
        elements=tuple(elements),
        grid_size=_TILE_SIZE,
    )
    return VisualView(
        "factorio:routed" if routed_view else "factorio:placed",
        "Factorio routed" if routed_view else "Factorio placement",
        (scene,),
    )


def _entity_element(
    entity: FactorioEntity,
    component_by_id: Mapping[str, object],
    routed: FactorioRoutedDesign,
) -> VisualElement:
    descriptor = _entity_descriptor(entity, routed)
    references = [VisualReference("factorio_entity", entity.identifier)]
    component = (
        component_by_id.get(entity.source_component)
        if entity.source_component is not None
        else None
    )
    source = getattr(component, "source", None)
    if isinstance(source, ObjectSubject):
        references.append(
            VisualReference("material_object", source.object.value)
        )
    elif isinstance(source, ModuleValueSubject):
        references.append(
            VisualReference(
                "module_value",
                f"{source.module}:{source.port}",
            )
        )
    references.extend(
        VisualReference("material_net", wire.net.value)
        for wire in routed.wires
        if entity.identifier in {wire.source.entity, wire.target.entity}
    )
    return VisualElement(
        f"factorio-entity:{entity.identifier}",
        descriptor,
        VisualTransform(
            entity.x * _TILE_SIZE,
            entity.y * _TILE_SIZE,
            entity.angle,
        ),
        tuple(dict.fromkeys(references)),
        layer=1,
    )


def _entity_descriptor(
    entity: FactorioEntity,
    routed: FactorioRoutedDesign,
) -> VisualElementDescriptor:
    profile = factorio_entity_profile(entity.prototype)
    bounds = VisualBounds.centered(
        profile.width * _TILE_SIZE,
        profile.height * _TILE_SIZE,
    )
    properties = [
        VisualProperty("Prototype", entity.prototype, "Entity"),
        VisualProperty("ID", entity.identifier, "Identity"),
    ]
    if not entity.configuration.is_empty:
        properties.append(
            VisualProperty(
                "Configuration",
                entity.configuration.canonical_json,
                "Entity",
            )
        )
    if entity.prototype == "arithmetic-combinator":
        configuration = FactorioObjectConfigurationCodec().decode(
            FACTORIO_ARITHMETIC_COMBINATOR,
            entity.configuration,
        )
        if not isinstance(configuration, FactorioArithmeticConfiguration):
            raise ValueError("Invalid Factorio arithmetic configuration")
        return _arithmetic_descriptor(configuration, properties=tuple(properties))
    if entity.prototype == "constant-combinator":
        configuration = entity.configuration.canonical_data()
        signal = configuration.get("signal")
        count = configuration.get("count")
        signal_label = (
            signal.removeprefix("signal-")
            if isinstance(signal, str)
            else "?"
        )
        label = f"{signal_label}={count}"
        fill = "#f2c14e"
        shape = VisualRectangle(
            bounds,
            VisualStyle(fill=fill, stroke="#574819", stroke_width=2.0),
        )
        direction = PortDirection.OUTPUT
        properties.extend(
            (
                VisualProperty("Signal", str(signal), "Input driver"),
                VisualProperty("Value", str(count), "Input driver"),
            )
        )
        primitives = (shape, VisualText(VisualPoint(0.0, 0.0), label))
    elif entity.prototype == "small-lamp":
        configuration = FactorioObjectConfigurationCodec().decode(
            FACTORIO_LAMP,
            entity.configuration,
        )
        if not isinstance(configuration, FactorioLampConfiguration):
            raise ValueError("Invalid Factorio lamp configuration")
        assignment = _lamp_signal(routed, entity.identifier)
        signal_label = assignment.signal.removeprefix("signal-")
        label = f"{signal_label} > 0"
        shape = VisualEllipse(
            bounds,
            VisualStyle(fill="#f4dc72", stroke="#625514", stroke_width=2.0),
        )
        direction = PortDirection.INPUT
        properties.extend(
            (
                VisualProperty("Signal", assignment.signal, "Lamp"),
                VisualProperty("Condition", "> 0", "Lamp"),
            )
        )
        primitives = (shape, VisualText(VisualPoint(0.0, 0.0), label))
    else:
        label = (
            "Big electric pole"
            if entity.prototype == "big-electric-pole"
            else "Medium electric pole"
        )
        fill = (
            "#6f7f83"
            if entity.prototype == "big-electric-pole"
            else "#93a3a7"
        )
        shape = VisualEllipse(
            bounds,
            VisualStyle(fill=fill, stroke="#26383b", stroke_width=2.0),
        )
        direction = PortDirection.INOUT
        primitives = (shape,)
    ports = tuple(
        VisualPort(
            f"connector:{connector.identifier}",
            str(connector.identifier),
            direction,
            VisualPoint(
                connector.x * _TILE_SIZE,
                connector.y * _TILE_SIZE,
            ),
        )
        for connector in profile.connectors
    )
    return VisualElementDescriptor(
        label,
        bounds,
        primitives,
        ports,
        tuple(properties),
    )


def _lamp_signal(
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
        raise ValueError(
            f"Factorio lamp {entity!r} has {len(matches)} incident signals"
        )
    return next(iter(matches))


def _arithmetic_descriptor(
    configuration: FactorioArithmeticConfiguration,
    *,
    properties: tuple[VisualProperty, ...] = (),
) -> VisualElementDescriptor:
    bounds = VisualBounds.centered(2.0 * _TILE_SIZE, _TILE_SIZE)
    return VisualElementDescriptor(
        configuration.operation.upper() + "32",
        bounds,
        (
            VisualRectangle(
                bounds,
                VisualStyle(fill="#c9d2d4", stroke="#26383b", stroke_width=2.0),
            ),
            VisualText(VisualPoint(0.0, 0.0), "+" if configuration.operation == "add" else configuration.operation.upper()),
        ),
        (
            VisualPort(
                "connector:1",
                "A/B",
                PortDirection.INPUT,
                VisualPoint(-0.5 * _TILE_SIZE, 0.0),
            ),
            VisualPort(
                "connector:2",
                "Y",
                PortDirection.OUTPUT,
                VisualPoint(0.5 * _TILE_SIZE, 0.0),
            ),
        ),
        (
            VisualProperty("Operation", configuration.operation, "Arithmetic"),
            VisualProperty("Width", str(configuration.y_width), "Arithmetic"),
            *properties,
        ),
    )


def _logical_arithmetic_descriptor(
    configuration: FactorioArithmeticConfiguration,
) -> VisualElementDescriptor:
    bounds = VisualBounds.centered(2.0, 1.0)
    inputs = tuple(
        (name, bit)
        for name, width in (
            ("a", configuration.a_width),
            ("b", configuration.b_width),
        )
        for bit in range(width)
    )
    outputs = tuple(("y", bit) for bit in range(configuration.y_width))
    ports = tuple(
        VisualPort(
            f"{name}[{bit}]",
            f"{name}[{configuration.a_width - 1 if name == 'a' else configuration.b_width - 1}:0]",
            PortDirection.INPUT,
            VisualPoint(bounds.min_x, -bounds.height / 4 if name == "a" else bounds.height / 4),
        )
        for name, bit in inputs
    ) + tuple(
        VisualPort(
            f"{name}[{bit}]",
            f"{name}[{configuration.y_width - 1}:0]",
            PortDirection.OUTPUT,
            VisualPoint(bounds.max_x, 0.0),
        )
        for name, bit in outputs
    )
    return VisualElementDescriptor(
        configuration.operation.upper() + "32",
        bounds,
        (
            VisualRectangle(
                bounds,
                VisualStyle(fill="#c9d2d4", stroke="#26383b", stroke_width=0.08),
            ),
            VisualText(VisualPoint(0.0, 0.0), "+" if configuration.operation == "add" else configuration.operation.upper()),
        ),
        ports,
        (
            VisualProperty("Operation", configuration.operation, "Arithmetic"),
            VisualProperty("Width", str(configuration.y_width), "Arithmetic"),
        ),
    )


def _logical_lamp_descriptor(
    configuration: FactorioLampConfiguration,
) -> VisualElementDescriptor:
    bounds = VisualBounds.centered(1.0, 1.0)
    return VisualElementDescriptor(
        "LAMP",
        bounds,
        (
            VisualEllipse(
                bounds,
                VisualStyle(fill="#f4dc72", stroke="#625514", stroke_width=0.08),
            ),
            VisualText(VisualPoint(0.0, 0.0), ">0"),
        ),
        (
            VisualPort(
                "in[0]",
                "in",
                PortDirection.INPUT,
                VisualPoint(bounds.min_x, 0.0),
            ),
        ),
        (
            VisualProperty("Comparator", configuration.comparator, "Lamp"),
            VisualProperty("Constant", str(configuration.constant), "Lamp"),
        ),
    )


def _wire_element(
    index: int,
    net: str,
    signal: str,
    color: FactorioWireColor,
    source: tuple[float, float],
    target: tuple[float, float],
    *,
    show_label: bool,
) -> VisualElement:
    source = (source[0] * _TILE_SIZE, source[1] * _TILE_SIZE)
    target = (target[0] * _TILE_SIZE, target[1] * _TILE_SIZE)
    min_x = min(source[0], target[0])
    min_y = min(source[1], target[1])
    max_x = max(source[0], target[0])
    max_y = max(source[1], target[1])
    bounds = VisualBounds(min_x, min_y, max_x, max_y)
    primitives: list[VisualPrimitive] = [
        VisualPolyline(
            (VisualPoint(*source), VisualPoint(*target)),
            VisualStyle(
                fill=None,
                stroke=_WIRE_COLORS[color],
                stroke_width=3.0,
            ),
        )
    ]
    if show_label:
        primitives.append(
            VisualText(
                VisualPoint(
                    (source[0] + target[0]) / 2,
                    (source[1] + target[1]) / 2 - 10.0,
                ),
                signal,
                _WIRE_COLORS[color],
            )
        )
    return VisualElement(
        f"factorio-wire:{index}",
        VisualElementDescriptor(
            f"{signal} {color.value}",
            bounds,
            tuple(primitives),
            properties=(
                VisualProperty("Signal", signal, "Wire"),
                VisualProperty("Color", color.value, "Wire"),
                VisualProperty("Net", net, "Wire"),
            ),
        ),
        VisualTransform(0.0, 0.0),
        (VisualReference("material_net", net),),
        layer=0,
        collision_enabled=False,
    )


def _power_wire_element(
    index: int,
    source_entity: str,
    target_entity: str,
    source: tuple[float, float],
    target: tuple[float, float],
) -> VisualElement:
    source_point = VisualPoint(source[0] * _TILE_SIZE, source[1] * _TILE_SIZE)
    target_point = VisualPoint(target[0] * _TILE_SIZE, target[1] * _TILE_SIZE)
    bounds = VisualBounds(
        min(source_point.x, target_point.x),
        min(source_point.y, target_point.y),
        max(source_point.x, target_point.x),
        max(source_point.y, target_point.y),
    )
    return VisualElement(
        f"factorio-power-wire:{index}",
        VisualElementDescriptor(
            "Copper power wire",
            bounds,
            (
                VisualPolyline(
                    (source_point, target_point),
                    VisualStyle(
                        fill=None,
                        stroke=_COPPER_WIRE_COLOR,
                        stroke_width=3.0,
                    ),
                ),
            ),
            properties=(
                VisualProperty("Source", source_entity, "Power"),
                VisualProperty("Target", target_entity, "Power"),
            ),
        ),
        VisualTransform(0.0, 0.0),
        (
            VisualReference("factorio_entity", source_entity),
            VisualReference("factorio_entity", target_entity),
        ),
        layer=0,
        collision_enabled=False,
    )


def _distributed(count: int, minimum: float, maximum: float) -> tuple[float, ...]:
    if count == 0:
        return ()
    if count == 1:
        return ((minimum + maximum) / 2,)
    padding = (maximum - minimum) / (2 * count)
    start = minimum + padding
    end = maximum - padding
    return tuple(
        start + index * (end - start) / (count - 1)
        for index in range(count)
    )


def _scene_bounds(entities: tuple[FactorioEntity, ...]) -> VisualBounds:
    if not entities:
        return VisualBounds(
            -_TILE_SIZE,
            -_TILE_SIZE,
            _TILE_SIZE,
            _TILE_SIZE,
        )
    bounds = [entity_bounds(item) for item in entities]
    return VisualBounds(
        (min(item[0] for item in bounds) - 1.0) * _TILE_SIZE,
        (min(item[1] for item in bounds) - 1.0) * _TILE_SIZE,
        (max(item[2] for item in bounds) + 1.0) * _TILE_SIZE,
        (max(item[3] for item in bounds) + 1.0) * _TILE_SIZE,
    )