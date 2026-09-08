from __future__ import annotations

from collections import defaultdict
from collections.abc import Mapping

from gateforge.graph import (
    ConstantSubject,
    MaterialGraph,
    MaterialSubject,
    ModulePortSubject,
    ObjectSubject,
)
from gateforge.material import (
    MaterialAttachment,
    MaterialConstantRef,
    MaterialDesign,
    MaterialModulePortRef,
    MaterialNetId,
    MaterialObject,
    MaterialObjectPortRef,
)
from gateforge.placement import PlacedContainer, PlacedDesign, PrefabPlacement
from gateforge.provider import TargetProvider
from gateforge.target import PortDirection
from gateforge.visualization.model import (
    VisualBounds,
    VisualDocument,
    VisualElement,
    VisualElementDescriptor,
    VisualEndpoint,
    VisualNet,
    VisualPoint,
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


_TERMINAL_SIZE = 42.0


def visual_port_id(name: str, bit: int = 0) -> str:
    return f"{name}[{bit}]"


def build_visual_document(
    material: MaterialDesign,
    graph: MaterialGraph,
    placement: PlacedDesign,
    providers: Mapping[str, TargetProvider],
    *,
    provider_options: Mapping[str, Mapping[str, object]] | None = None,
) -> VisualDocument:
    if graph.design != material:
        raise ValueError("Visualization graph does not contain the supplied material")
    views = [_build_material_view(material, graph, placement, providers)]
    options = provider_options or {}
    provider_ids = sorted(
        {
            *(item.type.provider for item in material.objects),
            *(item.type.provider for item in material.nets),
        }
    )
    request_base = {
        "material": material,
        "graph": graph,
        "placement": placement,
        "providers": providers,
    }
    for identifier in provider_ids:
        provider = providers[identifier]
        if provider.visualization is None:
            continue
        views.extend(
            provider.visualization.build_realized_views(
                VisualizationRequest(
                    **request_base,
                    options=options.get(identifier, {}),
                )
            )
        )
    return VisualDocument(tuple(views))


def _build_material_view(
    material: MaterialDesign,
    graph: MaterialGraph,
    placement: PlacedDesign,
    providers: Mapping[str, TargetProvider],
) -> VisualView:
    object_by_id = {item.identifier: item for item in material.objects}
    net_by_id = {item.identifier: item for item in material.nets}
    containers = {item.path: item for item in placement.containers}
    nets_by_container = _material_nets_by_container(graph, placement, net_by_id)
    placement_options = placement.provenance.options.canonical_data()
    row_pitch = placement_options.get("row_pitch")
    grid_size = (
        float(row_pitch)
        if isinstance(row_pitch, (int, float)) and not isinstance(row_pitch, bool)
        else None
    )
    scenes = []
    for container in placement.containers:
        elements = [
            _material_prefab_element(prefab, object_by_id, providers)
            for prefab in container.prefabs
        ]
        elements.extend(
            _material_child_element(containers[child])
            for child in container.children
        )
        elements.extend(_material_boundary_elements(container))
        scenes.append(
            VisualScene(
                container.path,
                container.name,
                _visual_bounds(container.board),
                tuple(elements),
                nets_by_container[container.path],
                parent=container.parent,
                grid_size=grid_size,
            )
        )
    return VisualView("material", "Material placement", tuple(scenes))


def _material_prefab_element(
    placed: PrefabPlacement,
    object_by_id: Mapping,
    providers: Mapping[str, TargetProvider],
) -> VisualElement:
    source = placed.source
    if isinstance(source, ObjectSubject):
        material_object = object_by_id[source.object]
        provider = providers[material_object.type.provider]
        descriptor = (
            provider.visualization.describe_material_object(
                material_object,
                provider.registry,
            )
            if provider.visualization is not None
            else _default_object_descriptor(material_object, provider)
        )
        identifier = f"object:{material_object.identifier.value}"
        references = (
            VisualReference("material_object", material_object.identifier.value),
        )
    elif isinstance(source, ModulePortSubject):
        identifier = _module_port_element_id(
            source.module,
            source.port,
            source.bit,
            source.direction,
        )
        descriptor = _terminal_descriptor(
            f"{source.port}[{source.bit}]",
            source.direction,
        )
        references = (
            VisualReference(
                "module_port",
                f"{source.module}:{source.port}[{source.bit}]",
            ),
        )
    else:
        identifier = _constant_element_id(source.net.value, source.value)
        descriptor = _constant_descriptor(source.value)
        references = (VisualReference("material_net", source.net.value),)
    return VisualElement(
        identifier,
        descriptor,
        VisualTransform(placed.x, placed.y, placed.angle),
        references,
    )


def _material_child_element(
    child: PlacedContainer,
) -> VisualElement:
    input_ports = tuple(
        item.index
        for item in child.boundary_ports
        if item.direction == PortDirection.INPUT
    )
    output_ports = tuple(
        item.index
        for item in child.boundary_ports
        if item.direction == PortDirection.OUTPUT
    )
    bounds = _visual_bounds(child.facade)
    ports = tuple(
        VisualPort(
            f"in:{port}",
            f"IN {port}",
            PortDirection.INPUT,
            VisualPoint(bounds.min_x, position),
        )
        for port, position in zip(
            input_ports,
            _distributed(len(input_ports), bounds.min_y, bounds.max_y),
        )
    ) + tuple(
        VisualPort(
            f"out:{port}",
            f"OUT {port}",
            PortDirection.OUTPUT,
            VisualPoint(bounds.max_x, position),
        )
        for port, position in zip(
            output_ports,
            _distributed(len(output_ports), bounds.min_y, bounds.max_y),
        )
    )
    return VisualElement(
        f"container:{child.path}",
        VisualElementDescriptor(
            child.name,
            bounds,
            (
                VisualRectangle(
                    bounds,
                    VisualStyle(fill="#c7d8d0", stroke="#1f3438", stroke_width=2.0),
                ),
                VisualText(VisualPoint(0.0, 0.0), child.name),
            ),
            ports,
            (
                VisualProperty("Path", child.path, "Container"),
                VisualProperty("Kind", child.kind.value, "Container"),
                VisualProperty(
                    "Board",
                    f"{child.board.width:g} x {child.board.height:g}",
                    "Container",
                ),
            ),
        ),
        VisualTransform(child.x, child.y, child.angle),
        (VisualReference("container", child.path),),
        layer=1,
        linked_scene=child.path,
    )


def _material_boundary_elements(
    container: PlacedContainer,
) -> tuple[VisualElement, ...]:
    result = []
    for boundary in container.boundary_ports:
        source = boundary.direction == PortDirection.INPUT
        prefix = "boundary-input" if source else "boundary-output"
        port_kind = "out" if source else "in"
        bounds = VisualBounds.centered(36.0, 24.0)
        result.append(
            VisualElement(
                f"{prefix}:{boundary.index}",
                VisualElementDescriptor(
                    str(boundary.index),
                    bounds,
                    (
                        VisualRectangle(
                            bounds,
                            VisualStyle(
                                fill="#f5c84c",
                                stroke="#705b16",
                                stroke_width=2.0,
                            ),
                        ),
                        VisualText(VisualPoint(0.0, 0.0), str(boundary.index)),
                    ),
                    (
                        VisualPort(
                            f"{port_kind}:{boundary.index}",
                            str(boundary.index),
                            (
                                PortDirection.OUTPUT
                                if source
                                else PortDirection.INPUT
                            ),
                            VisualPoint(
                                bounds.max_x if source else bounds.min_x,
                                0.0,
                            ),
                        ),
                    ),
                    (
                        VisualProperty(
                            "Direction",
                            boundary.direction.value,
                            "Boundary",
                        ),
                        VisualProperty("Net", boundary.net.value, "Boundary"),
                    ),
                ),
                VisualTransform(boundary.x, boundary.y, boundary.angle),
                (VisualReference("material_net", boundary.net.value),),
                layer=2,
            )
        )
    return tuple(result)


def _material_nets_by_container(
    graph: MaterialGraph,
    placement: PlacedDesign,
    net_by_id: Mapping,
) -> Mapping[str, tuple[VisualNet, ...]]:
    containers = {item.path: item for item in placement.containers}
    parents = {item.path: item.parent for item in placement.containers}
    subject_owners = {
        prefab.source: container.path
        for container in placement.containers
        for prefab in container.prefabs
    }
    grouped: dict[str, dict[MaterialNetId, set[VisualEndpoint]]] = {
        item.path: defaultdict(set) for item in placement.containers
    }
    for dependency in graph.dependencies:
        source_subject = _material_attachment_subject(
            dependency.net,
            dependency.source,
        )
        target_subject = _material_attachment_subject(
            dependency.net,
            dependency.target,
        )
        source_owner = subject_owners[source_subject]
        target_owner = subject_owners[target_subject]
        common = _lowest_common_container(source_owner, target_owner, parents)
        source_endpoint = _material_endpoint(
            dependency.net.value,
            dependency.source,
        )
        while source_owner != common:
            boundary = _boundary_endpoint(
                containers[source_owner],
                dependency.net,
                PortDirection.OUTPUT,
            )
            grouped[source_owner][dependency.net].update(
                (source_endpoint, boundary)
            )
            child = source_owner
            parent = parents[child]
            if parent is None:
                raise ValueError("Cannot route a material net above the root scene")
            source_endpoint = _child_endpoint(
                child,
                containers[child],
                dependency.net,
                PortDirection.OUTPUT,
            )
            source_owner = parent

        target_endpoint = _material_endpoint(
            dependency.net.value,
            dependency.target,
        )
        while target_owner != common:
            boundary = _boundary_endpoint(
                containers[target_owner],
                dependency.net,
                PortDirection.INPUT,
            )
            grouped[target_owner][dependency.net].update(
                (boundary, target_endpoint)
            )
            child = target_owner
            parent = parents[child]
            if parent is None:
                raise ValueError("Cannot route a material net below the root scene")
            target_endpoint = _child_endpoint(
                child,
                containers[child],
                dependency.net,
                PortDirection.INPUT,
            )
            target_owner = parent

        grouped[common][dependency.net].update(
            (source_endpoint, target_endpoint)
        )

    return {
        path: tuple(
            VisualNet(
                f"net:{identifier.value}",
                identifier.value[:12],
                tuple(endpoints),
                (
                    VisualProperty("ID", identifier.value, "Net"),
                    VisualProperty("Type", net_by_id[identifier].type.name, "Net"),
                    VisualProperty(
                        "Provider",
                        net_by_id[identifier].type.provider,
                        "Net",
                    ),
                ),
            )
            for identifier, endpoints in sorted(
                values.items(),
                key=lambda item: item[0].value,
            )
        )
        for path, values in grouped.items()
    }


def _boundary_endpoint(
    container: PlacedContainer,
    net: MaterialNetId,
    direction: PortDirection,
) -> VisualEndpoint:
    boundary = next(
        item
        for item in container.boundary_ports
        if item.net == net and item.direction == direction
    )
    if direction == PortDirection.INPUT:
        return VisualEndpoint(
            f"boundary-input:{boundary.index}",
            f"out:{boundary.index}",
        )
    return VisualEndpoint(
        f"boundary-output:{boundary.index}",
        f"in:{boundary.index}",
    )


def _child_endpoint(
    path: str,
    container: PlacedContainer,
    net: MaterialNetId,
    direction: PortDirection,
) -> VisualEndpoint:
    boundary = next(
        item
        for item in container.boundary_ports
        if item.net == net and item.direction == direction
    )
    port_kind = "in" if direction == PortDirection.INPUT else "out"
    return VisualEndpoint(
        f"container:{path}",
        f"{port_kind}:{boundary.index}",
    )


def _lowest_common_container(
    left: str,
    right: str,
    parents: Mapping[str, str | None],
) -> str:
    ancestors = set()
    cursor: str | None = left
    while cursor is not None:
        ancestors.add(cursor)
        cursor = parents[cursor]
    cursor = right
    while cursor not in ancestors:
        parent = parents[cursor]
        if parent is None:
            raise ValueError("Placed scenes do not share a root container")
        cursor = parent
    return cursor


def _material_attachment_subject(
    net: MaterialNetId,
    attachment: MaterialAttachment,
) -> MaterialSubject:
    if isinstance(attachment, MaterialObjectPortRef):
        return ObjectSubject(attachment.object)
    if isinstance(attachment, MaterialModulePortRef):
        return ModulePortSubject(
            attachment.module,
            attachment.port,
            attachment.bit,
            attachment.direction,
        )
    if isinstance(attachment, MaterialConstantRef):
        return ConstantSubject(net, attachment.value)
    raise TypeError(f"Unknown material attachment {attachment!r}")


def _visual_bounds(bounds) -> VisualBounds:
    return VisualBounds(bounds.min_x, bounds.min_y, bounds.max_x, bounds.max_y)


def _distributed(count: int, minimum: float, maximum: float) -> tuple[float, ...]:
    if count <= 0:
        return ()
    step = (maximum - minimum) / (count + 1)
    return tuple(minimum + step * (index + 1) for index in range(count))


def _default_object_descriptor(
    material_object: MaterialObject,
    provider: TargetProvider,
) -> VisualElementDescriptor:
    geometry = provider.object_placement_geometry(material_object)
    width = 84.0 if geometry is None else geometry.width
    height = 52.5 if geometry is None else geometry.height
    bounds = VisualBounds.centered(width, height)
    schema = provider.registry.object(material_object.type)
    ports = _schema_ports(schema.ports, bounds)
    return VisualElementDescriptor(
        label=material_object.role,
        bounds=bounds,
        primitives=(
            VisualRectangle(
                bounds,
                VisualStyle(fill="#dfe7e8", stroke="#40565c", stroke_width=2.0),
            ),
            VisualText(VisualPoint(0.0, 0.0), material_object.role),
        ),
        ports=ports,
        properties=_material_object_properties(material_object),
    )


def _schema_ports(port_schemas, bounds: VisualBounds) -> tuple[VisualPort, ...]:
    expanded = tuple(
        (port, bit)
        for port in sorted(port_schemas, key=lambda item: (item.direction.value, item.name))
        for bit in range(port.width)
    )
    by_direction = {
        direction: tuple(item for item in expanded if item[0].direction == direction)
        for direction in PortDirection
    }
    result: list[VisualPort] = []
    for direction, values in by_direction.items():
        for index, (port, bit) in enumerate(values):
            fraction = (index + 1) / (len(values) + 1)
            if direction == PortDirection.INPUT:
                position = VisualPoint(
                    bounds.min_x,
                    bounds.min_y + fraction * bounds.height,
                )
            elif direction == PortDirection.OUTPUT:
                position = VisualPoint(
                    bounds.max_x,
                    bounds.min_y + fraction * bounds.height,
                )
            else:
                position = VisualPoint(
                    bounds.min_x + fraction * bounds.width,
                    bounds.max_y,
                )
            result.append(
                VisualPort(
                    visual_port_id(port.name, bit),
                    port.name if port.width == 1 else f"{port.name}[{bit}]",
                    direction,
                    position,
                )
            )
    return tuple(result)


def _material_object_properties(
    material_object: MaterialObject,
) -> tuple[VisualProperty, ...]:
    return (
        VisualProperty("Role", material_object.role, "Object"),
        VisualProperty("Type", material_object.type.name, "Object"),
        VisualProperty("Provider", material_object.type.provider, "Object"),
        VisualProperty("Hierarchy", material_object.hierarchy, "Object"),
        VisualProperty("ID", material_object.identifier.value, "Identity"),
        VisualProperty("Occurrence", material_object.occurrence.value, "Identity"),
        VisualProperty("Configuration", material_object.configuration.canonical_json, "Object"),
    )


def _terminal_descriptor(label: str, direction: PortDirection) -> VisualElementDescriptor:
    bounds = VisualBounds.centered(_TERMINAL_SIZE, _TERMINAL_SIZE)
    port_position = (
        VisualPoint(bounds.max_x, 0.0)
        if direction == PortDirection.INPUT
        else VisualPoint(bounds.min_x, 0.0)
    )
    return VisualElementDescriptor(
        label=label,
        bounds=bounds,
        primitives=(
            VisualRectangle(
                bounds,
                VisualStyle(fill="#f5c84c", stroke="#705b16", stroke_width=2.0),
            ),
            VisualText(VisualPoint(0.0, 0.0), label),
        ),
        ports=(VisualPort("signal", label, direction, port_position),),
        properties=(VisualProperty("Direction", direction.value, "Port"),),
    )


def _constant_descriptor(value: str) -> VisualElementDescriptor:
    bounds = VisualBounds.centered(_TERMINAL_SIZE, _TERMINAL_SIZE)
    return VisualElementDescriptor(
        label=value,
        bounds=bounds,
        primitives=(
            VisualRectangle(
                bounds,
                VisualStyle(fill="#f7dc78", stroke="#705b16", stroke_width=2.0),
            ),
            VisualText(VisualPoint(0.0, 0.0), value),
        ),
        ports=(
            VisualPort(
                "signal",
                value,
                PortDirection.OUTPUT,
                VisualPoint(bounds.max_x, 0.0),
            ),
        ),
        properties=(VisualProperty("Value", value, "Constant"),),
    )


def _material_endpoint(net: str, attachment: MaterialAttachment) -> VisualEndpoint:
    if isinstance(attachment, MaterialObjectPortRef):
        return VisualEndpoint(
            f"object:{attachment.object.value}",
            visual_port_id(attachment.port, attachment.bit),
        )
    if isinstance(attachment, MaterialModulePortRef):
        return VisualEndpoint(
            _module_port_element_id(
                attachment.module,
                attachment.port,
                attachment.bit,
                attachment.direction,
            ),
            "signal",
        )
    if isinstance(attachment, MaterialConstantRef):
        return VisualEndpoint(_constant_element_id(net, attachment.value), "signal")
    raise TypeError(f"Unsupported material attachment {attachment!r}")


def _module_port_element_id(
    module: str,
    port: str,
    bit: int,
    direction: PortDirection,
) -> str:
    return f"module-port:{module}:{port}:{bit}:{direction.value}"


def _constant_element_id(net: str, value: str) -> str:
    return f"constant:{net}:{value}"