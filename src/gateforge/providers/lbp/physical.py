from __future__ import annotations

from collections import Counter, defaultdict
from collections.abc import Mapping
from dataclasses import asdict

from gateforge.graph import (
    ConstantSubject,
    MaterialGraph,
    MaterialSubject,
    ModulePortSubject,
    ObjectSubject,
)
from gateforge.hierarchy import GeneratedHierarchyPolicy, PhysicalHierarchyPolicy
from gateforge.placement.hierarchy import (
    BoundaryPortSubject,
    ChildContainerSubject,
    LocalDependencyEndpoint,
    build_placement_hierarchy,
)
from gateforge.placement.model import PhysicalBounds, PlacementError
from gateforge.placement.physical import (
    PhysicalAnnotation,
    PhysicalBoundaryPort,
    PhysicalComponent,
    PhysicalConnection,
    PhysicalContainer,
    PhysicalDesign,
    PhysicalEndpoint,
    PhysicalEndpointKind,
    PhysicalLayoutRules,
    PhysicalMember,
    PhysicalPrefabInstance,
)
from gateforge.provider import TargetProvider
from gateforge.providers.lbp.common import LBP_PROVIDER
from gateforge.providers.lbp.plan import (
    LbpGadget,
    LbpGadgetKind,
    LbpGadgetSource,
)
from gateforge.providers.lbp.realize import (
    lbp_attachment_endpoint,
    lbp_constant_gadget_id,
    lbp_module_gadget_id,
    lbp_object_gadget_id,
    realize_lbp_material_gadget,
)
from gateforge.target import PortDirection, ProviderConfiguration


_GATE_SCALE_X = 1.4666667
_GATE_SCALE_STEP_Y = 0.73333334
_NOTE_SCALE = 1.4666667
_NOTE_OFFSET_X = 105.0
_BASE_SIZE = 52.5


def elaborate_lbp_physical_design(
    graph: MaterialGraph,
    providers: Mapping[str, TargetProvider],
    physical_hierarchy: PhysicalHierarchyPolicy = PhysicalHierarchyPolicy(),
    generated_hierarchy: GeneratedHierarchyPolicy = GeneratedHierarchyPolicy(),
) -> PhysicalDesign:
    topology = build_placement_hierarchy(
        graph,
        providers,
        physical_hierarchy,
        generated_hierarchy,
    )
    components_by_owner: dict[str, list[PhysicalComponent]] = defaultdict(list)
    annotations_by_owner: dict[str, list[PhysicalAnnotation]] = defaultdict(list)
    prefabs_by_owner: dict[str, list[PhysicalPrefabInstance]] = defaultdict(list)

    for material_object in graph.design.objects:
        subject = ObjectSubject(material_object.identifier)
        gadget, scale_x, scale_y = realize_lbp_material_gadget(material_object)
        provider = providers.get(material_object.type.provider)
        geometry = (
            None
            if provider is None
            else provider.object_placement_geometry(material_object)
        )
        bounds = PhysicalBounds.centered(
            _BASE_SIZE if geometry is None else geometry.width,
            _BASE_SIZE if geometry is None else geometry.height,
        )
        component = _physical_component(
            gadget,
            subject,
            bounds,
            scale_x,
            scale_y,
        )
        _add_prefab(
            topology.subject_owners[subject],
            subject,
            component,
            (),
            components_by_owner,
            annotations_by_owner,
            prefabs_by_owner,
        )

    module_subjects = tuple(
        item for item in graph.subjects if isinstance(item, ModulePortSubject)
    )
    port_counts = Counter((item.module, item.port) for item in module_subjects)
    for subject in module_subjects:
        identifier = lbp_module_gadget_id(
            subject.module,
            subject.port,
            subject.bit,
            subject.direction,
        )
        name = (
            subject.port
            if port_counts[(subject.module, subject.port)] == 1
            else f"{subject.port}[{subject.bit}]"
        )
        gadget = LbpGadget(
            identifier=identifier,
            kind=LbpGadgetKind.NOT,
            source=LbpGadgetSource.MODULE_PORT,
            arity=1,
            inverted=False,
            name=name,
        )
        component = _physical_component(
            gadget,
            subject,
            PhysicalBounds.centered(_BASE_SIZE, _BASE_SIZE),
            _GATE_SCALE_X,
            _GATE_SCALE_STEP_Y * 2,
        )
        note_x = (
            -_NOTE_OFFSET_X
            if subject.direction == PortDirection.INPUT
            else _NOTE_OFFSET_X
        )
        annotation = PhysicalAnnotation(
            identifier=_module_note_id(subject),
            provider=LBP_PROVIDER,
            text=name,
            bounds=PhysicalBounds.centered(
                max(_BASE_SIZE, len(name) * 13.125),
                _BASE_SIZE,
            ),
            scale_x=_NOTE_SCALE,
            scale_y=_NOTE_SCALE,
        )
        _add_prefab(
            topology.subject_owners[subject],
            subject,
            component,
            ((annotation, note_x, 0.0),),
            components_by_owner,
            annotations_by_owner,
            prefabs_by_owner,
        )

    for subject in graph.subjects:
        if not isinstance(subject, ConstantSubject):
            continue
        if subject.value not in {"0", "1"}:
            raise PlacementError(
                f"LBP physical design does not support constant {subject.value!r} "
                f"on material net {subject.net.value}"
            )
        gadget = LbpGadget(
            identifier=lbp_constant_gadget_id(subject.net, subject.value),
            kind=LbpGadgetKind.BATTERY,
            source=LbpGadgetSource.CONSTANT,
            arity=0,
            manual_activation=subject.value == "1",
        )
        component = _physical_component(
            gadget,
            subject,
            PhysicalBounds.centered(_BASE_SIZE, _BASE_SIZE),
            _GATE_SCALE_X,
            _GATE_SCALE_X,
        )
        _add_prefab(
            topology.subject_owners[subject],
            subject,
            component,
            (),
            components_by_owner,
            annotations_by_owner,
            prefabs_by_owner,
        )

    boundary_by_container: dict[
        str,
        dict[tuple[object, PortDirection], PhysicalBoundaryPort],
    ] = {}
    for container in topology.containers:
        boundaries = tuple(
            PhysicalBoundaryPort(item.net, item.direction, index)
            for direction in (PortDirection.INPUT, PortDirection.OUTPUT)
            for index, item in enumerate(
                boundary
                for boundary in container.boundary_ports
                if boundary.direction == direction
            )
        )
        boundary_by_container[container.path] = {
            (item.net, item.direction): item for item in boundaries
        }

    physical_containers = []
    for container in topology.containers:
        boundaries = tuple(boundary_by_container[container.path].values())
        connections = tuple(
            PhysicalConnection(
                dependency.net,
                _physical_endpoint(
                    container.path,
                    dependency.source,
                    dependency.net,
                    True,
                    boundary_by_container,
                ),
                _physical_endpoint(
                    container.path,
                    dependency.target,
                    dependency.net,
                    False,
                    boundary_by_container,
                ),
            )
            for dependency in container.dependencies
        )
        physical_containers.append(
            PhysicalContainer(
                path=container.path,
                kind=container.kind,
                name=container.name,
                parent=container.parent,
                children=container.children,
                components=tuple(components_by_owner[container.path]),
                annotations=tuple(annotations_by_owner[container.path]),
                prefabs=tuple(prefabs_by_owner[container.path]),
                boundary_ports=boundaries,
                connections=connections,
            )
        )
    return PhysicalDesign(
        graph.design.get_digest(),
        LBP_PROVIDER,
        topology.root,
        tuple(physical_containers),
        topology,
        PhysicalLayoutRules(
            board_grid=52.5,
            board_margin_x=105.0,
            board_margin_y=105.0,
            minimum_board_half_width=420.0,
            minimum_board_half_height=262.5,
            facade_width=105.0,
            facade_port_pitch=26.25,
            facade_min_ports=2,
        ),
    )


def _physical_component(
    gadget: LbpGadget,
    subject: MaterialSubject,
    bounds: PhysicalBounds,
    scale_x: float,
    scale_y: float,
) -> PhysicalComponent:
    return PhysicalComponent(
        identifier=gadget.identifier.value,
        provider=LBP_PROVIDER,
        kind=gadget.kind.value,
        source=subject,
        bounds=bounds,
        payload=ProviderConfiguration.from_canonical_data(
            {
                "source": gadget.source.value,
                "arity": gadget.arity,
                "inverted": gadget.inverted,
                "name": gadget.name,
                "manual_activation": gadget.manual_activation,
                "output_arity": gadget.output_arity,
                "settings": asdict(gadget.settings),
                "scale_x": scale_x,
                "scale_y": scale_y,
            }
        ),
    )


def _add_prefab(
    owner: str,
    subject: MaterialSubject,
    component: PhysicalComponent,
    annotation_members: tuple[tuple[PhysicalAnnotation, float, float], ...],
    components_by_owner: dict[str, list[PhysicalComponent]],
    annotations_by_owner: dict[str, list[PhysicalAnnotation]],
    prefabs_by_owner: dict[str, list[PhysicalPrefabInstance]],
) -> None:
    annotations = tuple(item[0] for item in annotation_members)
    component_member = PhysicalMember(component.identifier)
    annotation_placements = tuple(
        PhysicalMember(item.identifier, x, y)
        for item, x, y in annotation_members
    )
    bounds = _prefab_bounds(
        ((component.bounds, component_member),),
        tuple(
            (annotation.bounds, member)
            for annotation, member in zip(annotations, annotation_placements)
        ),
    )
    components_by_owner[owner].append(component)
    annotations_by_owner[owner].extend(annotations)
    prefabs_by_owner[owner].append(
        PhysicalPrefabInstance(
            f"prefab:{component.identifier}",
            subject,
            bounds,
            (component_member,),
            annotation_placements,
        )
    )


def _prefab_bounds(
    *groups: tuple[tuple[PhysicalBounds, PhysicalMember], ...],
) -> PhysicalBounds:
    members = [item for group in groups for item in group]
    return PhysicalBounds(
        min(bounds.min_x + member.x for bounds, member in members),
        min(bounds.min_y + member.y for bounds, member in members),
        max(bounds.max_x + member.x for bounds, member in members),
        max(bounds.max_y + member.y for bounds, member in members),
    )


def _physical_endpoint(
    container: str,
    endpoint: LocalDependencyEndpoint,
    net,
    source: bool,
    boundaries: Mapping[
        str,
        Mapping[tuple[object, PortDirection], PhysicalBoundaryPort],
    ],
) -> PhysicalEndpoint:
    subject = endpoint.subject
    if endpoint.attachment is not None:
        realized = lbp_attachment_endpoint(
            endpoint.attachment,
            net,
            source=source,
        )
        return PhysicalEndpoint(
            PhysicalEndpointKind.COMPONENT,
            realized.gadget.value,
            realized.port,
        )
    if isinstance(subject, BoundaryPortSubject):
        boundary = boundaries[container][(subject.net, subject.direction)]
        return PhysicalEndpoint(
            PhysicalEndpointKind.BOUNDARY,
            container,
            boundary.index,
        )
    if isinstance(subject, ChildContainerSubject):
        direction = PortDirection.OUTPUT if source else PortDirection.INPUT
        boundary = boundaries[subject.path][(net, direction)]
        return PhysicalEndpoint(
            PhysicalEndpointKind.CHILD,
            subject.path,
            boundary.index + (0 if source else 1),
        )
    raise PlacementError(f"Physical dependency endpoint lacks attachment: {subject!r}")


def _module_note_id(subject: ModulePortSubject) -> str:
    return (
        f"module-note:{subject.module}:{subject.port}:{subject.bit}:"
        f"{subject.direction.value}"
    )