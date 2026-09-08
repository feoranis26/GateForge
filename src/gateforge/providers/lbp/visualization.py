from __future__ import annotations

from collections import defaultdict
from dataclasses import asdict
import json

from gateforge.graph import ObjectSubject
from gateforge.material import MaterialObject
from gateforge.placement import (
    ComponentPlacement,
    PhysicalBounds,
    PhysicalEndpoint,
    PhysicalEndpointKind,
    PlacedContainer,
)
from gateforge.providers.lbp.plan import (
    LbpGadget,
    LbpGadgetKind,
)
from gateforge.providers.lbp.realize import decode_lbp_component
from gateforge.providers.lbp.types import decode_lbp_object_type
from gateforge.target import PortDirection, TargetTypeRegistry
from gateforge.visualization.model import (
    VisualBounds,
    VisualElement,
    VisualElementDescriptor,
    VisualEllipse,
    VisualEndpoint,
    VisualNet,
    VisualPoint,
    VisualPolygon,
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


_PORT_COLOR = "#f4d35e"
_GADGET_COLORS = {
    LbpGadgetKind.NOT: "#eef2f3",
    LbpGadgetKind.AND: "#7bc8a4",
    LbpGadgetKind.OR: "#74b9d8",
    LbpGadgetKind.XOR: "#cf9ee8",
    LbpGadgetKind.BATTERY: "#f4d35e",
    LbpGadgetKind.TIMER: "#f29e4c",
    LbpGadgetKind.COUNTER: "#e76f51",
    LbpGadgetKind.RANDOMIZER: "#8ecae6",
    LbpGadgetKind.SELECTOR: "#a8dadc",
}


class LBPVisualizationAdapter:
    def describe_material_object(
        self,
        material_object: MaterialObject,
        registry: TargetTypeRegistry,
    ) -> VisualElementDescriptor:
        object_type = decode_lbp_object_type(material_object.type)
        geometry = object_type.get_placement_geometry()
        bounds = VisualBounds.centered(geometry.width, geometry.height)
        type_name = object_type.type_path()[-1].key
        return VisualElementDescriptor(
            label=type_name,
            bounds=bounds,
            primitives=_body_primitives(type_name, bounds, _material_color(type_name)),
            ports=_schema_ports(registry.object(material_object.type).ports, bounds),
            properties=(
                VisualProperty("Role", material_object.role, "Object"),
                VisualProperty("Type", material_object.type.name, "Object"),
                VisualProperty("Hierarchy", material_object.hierarchy, "Object"),
                VisualProperty(
                    "Configuration",
                    material_object.configuration.canonical_json,
                    "Object",
                ),
                VisualProperty("ID", material_object.identifier.value, "Identity"),
                VisualProperty(
                    "Occurrence",
                    material_object.occurrence.value,
                    "Identity",
                ),
            ),
        )

    def build_realized_views(
        self,
        request: VisualizationRequest,
    ) -> tuple[VisualView, ...]:
        return (_placed_view(request),)


def _placed_view(request: VisualizationRequest) -> VisualView:
    placement = request.placement
    containers = {item.path: item for item in placement.containers}
    scenes = []
    for container in placement.containers:
        elements = [
            _component_element(component, request)
            for component in container.components
        ]
        elements.extend(
            _note_element(
                annotation.identifier,
                annotation.text,
                annotation.bounds,
                annotation.x,
                annotation.y,
                annotation.angle,
            )
            for annotation in container.annotations
        )
        elements.extend(
            _microchip_element(containers[child])
            for child in container.children
        )
        elements.extend(_board_port_elements(container))
        scenes.append(
            VisualScene(
                identifier=container.path,
                label=container.name,
                bounds=_visual_bounds(container.board),
                elements=tuple(elements),
                nets=_physical_nets(container),
                parent=container.parent,
                grid_size=52.5,
            )
        )
    return VisualView("lbp:realized", "LBP realization", scenes)


def _component_element(
    component: ComponentPlacement,
    request: VisualizationRequest,
) -> VisualElement:
    gadget, _, _ = decode_lbp_component(component)
    bounds = _visual_bounds(component.bounds)
    label = gadget.name or _gadget_label(gadget)
    properties = [
        VisualProperty("Kind", gadget.kind.value, "Gadget"),
        VisualProperty("Source", gadget.source.value, "Gadget"),
        VisualProperty("Inputs", str(gadget.input_count), "Gadget"),
        VisualProperty("Outputs", str(gadget.output_count), "Gadget"),
        VisualProperty("Inverted", str(gadget.inverted).lower(), "Gadget"),
        VisualProperty("ID", gadget.identifier.value, "Identity"),
    ]
    properties.extend(
        VisualProperty(name, _format_property(value), "Settings")
        for name, value in asdict(gadget.settings).items()
    )
    references = [VisualReference("lbp_gadget", gadget.identifier.value)]
    material_objects = {item.identifier: item for item in request.material.objects}
    material_object = (
        material_objects.get(component.source.object)
        if isinstance(component.source, ObjectSubject)
        else None
    )
    if material_object is not None:
        references.append(
            VisualReference("material_object", material_object.identifier.value)
        )
        properties.insert(0, VisualProperty("Role", material_object.role, "Object"))
    return VisualElement(
        f"gadget:{gadget.identifier.value}",
        VisualElementDescriptor(
            label,
            bounds,
            _gadget_primitives(gadget, bounds, label),
            _gadget_ports(gadget, bounds),
            tuple(properties),
        ),
        VisualTransform(
            component.x,
            component.y,
            component.angle,
        ),
        tuple(references),
    )


def _gadget_primitives(
    gadget: LbpGadget,
    bounds: VisualBounds,
    label: str,
) -> tuple:
    color = _GADGET_COLORS[gadget.kind]
    style = VisualStyle(fill=color, stroke="#1f3438", stroke_width=2.0)
    if gadget.kind == LbpGadgetKind.NOT:
        inset = min(bounds.width, bounds.height) * 0.12
        shape = VisualPolygon(
            (
                VisualPoint(bounds.min_x + inset, bounds.min_y + inset),
                VisualPoint(bounds.min_x + inset, bounds.max_y - inset),
                VisualPoint(bounds.max_x - inset, 0.0),
            ),
            style,
        )
        return (shape, VisualText(VisualPoint(0.0, bounds.max_y + 10.0), label))
    if gadget.kind in {LbpGadgetKind.TIMER, LbpGadgetKind.COUNTER}:
        shape = VisualEllipse(bounds, style)
    else:
        shape = VisualRectangle(bounds, style)
    return (shape, VisualText(VisualPoint(0.0, 0.0), label))


def _body_primitives(
    label: str,
    bounds: VisualBounds,
    color: str,
) -> tuple:
    return (
        VisualRectangle(
            bounds,
            VisualStyle(fill=color, stroke="#1f3438", stroke_width=2.0),
        ),
        VisualText(VisualPoint(0.0, 0.0), label),
    )


def _gadget_ports(
    gadget: LbpGadget,
    bounds: VisualBounds,
) -> tuple[VisualPort, ...]:
    result: list[VisualPort] = []
    for index, y in enumerate(_distributed(gadget.input_count, bounds.min_y, bounds.max_y)):
        result.append(
            VisualPort(
                f"in:{index}",
                f"IN {index}",
                PortDirection.INPUT,
                VisualPoint(bounds.min_x, y),
            )
        )
    for index, y in enumerate(_distributed(gadget.output_count, bounds.min_y, bounds.max_y)):
        result.append(
            VisualPort(
                f"out:{index}",
                f"OUT {index}",
                PortDirection.OUTPUT,
                VisualPoint(bounds.max_x, y),
            )
        )
    return tuple(result)


def _microchip_element(container: PlacedContainer) -> VisualElement:
    inputs = tuple(
        item
        for item in container.boundary_ports
        if item.direction == PortDirection.INPUT
    )
    outputs = tuple(
        item
        for item in container.boundary_ports
        if item.direction == PortDirection.OUTPUT
    )
    bounds = _visual_bounds(container.facade)
    ports: list[VisualPort] = []
    for boundary, y in zip(
        inputs,
        _distributed(len(inputs), bounds.min_y, bounds.max_y),
    ):
        ports.append(
            VisualPort(
                f"in:{boundary.index + 1}",
                f"IN {boundary.index}",
                PortDirection.INPUT,
                VisualPoint(bounds.min_x, y),
            )
        )
    for boundary, y in zip(
        outputs,
        _distributed(len(outputs), bounds.min_y, bounds.max_y),
    ):
        ports.append(
            VisualPort(
                f"out:{boundary.index}",
                f"OUT {boundary.index}",
                PortDirection.OUTPUT,
                VisualPoint(bounds.max_x, y),
            )
        )
    return VisualElement(
        f"microchip:{container.path}",
        VisualElementDescriptor(
            container.name,
            bounds,
            _body_primitives(container.name, bounds, "#86a873"),
            tuple(ports),
            (
                VisualProperty("Path", container.path, "Microchip"),
                VisualProperty("Inputs", str(len(inputs)), "Microchip"),
                VisualProperty("Outputs", str(len(outputs)), "Microchip"),
                VisualProperty(
                    "Board",
                    f"{container.board.width:g} x {container.board.height:g}",
                    "Microchip",
                ),
            ),
        ),
        VisualTransform(container.x, container.y, container.angle),
        (VisualReference("container", container.path),),
        layer=1,
        linked_scene=container.path,
    )


def _board_port_elements(container: PlacedContainer) -> tuple[VisualElement, ...]:
    result: list[VisualElement] = []
    for boundary in container.boundary_ports:
        output = boundary.direction == PortDirection.INPUT
        port_id = f"out:{boundary.index}" if output else f"in:{boundary.index}"
        element_id = (
            f"board-input:{boundary.index}"
            if output
            else f"board-output:{boundary.index}"
        )
        bounds = VisualBounds.centered(36.0, 24.0)
        port_position = VisualPoint(
            bounds.max_x if output else bounds.min_x,
            0.0,
        )
        result.append(
            VisualElement(
                element_id,
                VisualElementDescriptor(
                    f"{boundary.direction.value} {boundary.index}",
                    bounds,
                    _body_primitives(str(boundary.index), bounds, _PORT_COLOR),
                    (
                        VisualPort(
                            port_id,
                            str(boundary.index),
                            PortDirection.OUTPUT if output else PortDirection.INPUT,
                            port_position,
                        ),
                    ),
                    (
                        VisualProperty(
                            "Direction",
                            boundary.direction.value,
                            "Board port",
                        ),
                        VisualProperty("Index", str(boundary.index), "Board port"),
                        VisualProperty("Net", boundary.net.value, "Board port"),
                    ),
                ),
                VisualTransform(boundary.x, boundary.y, boundary.angle),
                (VisualReference("material_net", boundary.net.value),),
                layer=2,
            )
        )
    return tuple(result)


def _note_element(
    identifier: str,
    text: str,
    physical_bounds: PhysicalBounds,
    x: float,
    y: float,
    angle: float,
) -> VisualElement:
    bounds = _visual_bounds(physical_bounds)
    return VisualElement(
        f"note:{identifier}",
        VisualElementDescriptor(
            text,
            bounds,
            (
                VisualRectangle(
                    bounds,
                    VisualStyle(fill="#fff35c", stroke="#8a7d00"),
                ),
                VisualText(VisualPoint(0.0, 0.0), text),
            ),
            properties=(VisualProperty("Text", text, "Note"),),
        ),
        VisualTransform(x, y, angle),
        (VisualReference("lbp_note", identifier),),
        layer=3,
        collision_enabled=False,
    )


def _physical_nets(container: PlacedContainer) -> tuple[VisualNet, ...]:
    grouped: dict[str, set[VisualEndpoint]] = defaultdict(set)
    for connection in container.connections:
        identifier = connection.net.value
        grouped[identifier].update(
            (
                _physical_visual_endpoint(connection.source, source=True),
                _physical_visual_endpoint(connection.target, source=False),
            )
        )
    return tuple(
        VisualNet(
            f"net:{identifier}",
            identifier,
            tuple(endpoints),
            (VisualProperty("Net", identifier, "Connection"),),
        )
        for identifier, endpoints in sorted(grouped.items())
    )


def _physical_visual_endpoint(
    endpoint: PhysicalEndpoint,
    *,
    source: bool,
) -> VisualEndpoint:
    port_kind = "out" if source else "in"
    if endpoint.kind == PhysicalEndpointKind.COMPONENT:
        return VisualEndpoint(
            f"gadget:{endpoint.identifier}",
            f"{port_kind}:{endpoint.port}",
        )
    if endpoint.kind == PhysicalEndpointKind.CHILD:
        return VisualEndpoint(
            f"microchip:{endpoint.identifier}",
            f"{port_kind}:{endpoint.port}",
        )
    element = "board-input" if source else "board-output"
    return VisualEndpoint(
        f"{element}:{endpoint.port}",
        f"{port_kind}:{endpoint.port}",
    )


def _visual_bounds(bounds: PhysicalBounds) -> VisualBounds:
    return VisualBounds(bounds.min_x, bounds.min_y, bounds.max_x, bounds.max_y)


def _schema_ports(port_schemas, bounds: VisualBounds) -> tuple[VisualPort, ...]:
    inputs = tuple(
        (port, bit)
        for port in sorted(port_schemas, key=lambda item: item.name)
        if port.direction == PortDirection.INPUT
        for bit in range(port.width)
    )
    outputs = tuple(
        (port, bit)
        for port in sorted(port_schemas, key=lambda item: item.name)
        if port.direction == PortDirection.OUTPUT
        for bit in range(port.width)
    )
    result: list[VisualPort] = []
    for direction, values, x in (
        (PortDirection.INPUT, inputs, bounds.min_x),
        (PortDirection.OUTPUT, outputs, bounds.max_x),
    ):
        for (port, bit), y in zip(values, _distributed(len(values), bounds.min_y, bounds.max_y)):
            result.append(
                VisualPort(
                    f"{port.name}[{bit}]",
                    port.name if port.width == 1 else f"{port.name}[{bit}]",
                    direction,
                    VisualPoint(x, y),
                )
            )
    return tuple(result)


def _distributed(count: int, minimum: float, maximum: float) -> tuple[float, ...]:
    if count == 0:
        return ()
    padding = min(13.125, (maximum - minimum) / (2 * count))
    start = minimum + padding
    end = maximum - padding
    if count == 1:
        return ((start + end) / 2,)
    return tuple(start + index * (end - start) / (count - 1) for index in range(count))


def _gadget_label(gadget: LbpGadget) -> str:
    if gadget.kind == LbpGadgetKind.BATTERY:
        return "1" if gadget.manual_activation else "0"
    label = gadget.kind.value
    return f"N{label}" if gadget.inverted else label


def _material_color(type_name: str) -> str:
    return {
        "NOT": "#eef2f3",
        "AND": "#7bc8a4",
        "OR": "#74b9d8",
        "XOR": "#cf9ee8",
        "TIMER": "#f29e4c",
        "COUNTER": "#e76f51",
        "RANDOMIZER": "#8ecae6",
        "SELECTOR": "#a8dadc",
        "PHASE_SELECTOR": "#a8dadc",
        "STORAGE_SELECTOR": "#a8dadc",
    }.get(type_name, "#dfe7e8")


def _format_property(value: object) -> str:
    if isinstance(value, (dict, list, tuple)):
        return json.dumps(value, sort_keys=True)
    if isinstance(value, bool):
        return str(value).lower()
    return str(value)