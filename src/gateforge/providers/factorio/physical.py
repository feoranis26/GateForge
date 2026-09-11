from collections.abc import Mapping

from gateforge.graph import (
    ConstantSubject,
    MaterialGraph,
    MaterialSubject,
    ModuleValueSubject,
    ObjectSubject,
)
from gateforge.hierarchy import GeneratedHierarchyPolicy, PhysicalHierarchyPolicy
from gateforge.material import (
    MaterialAttachment,
    MaterialModuleValueRef,
    MaterialObjectPortRef,
)
from gateforge.placement.hierarchy import (
    LocalDependencyEndpoint,
    build_placement_hierarchy,
)
from gateforge.placement.model import PhysicalBounds, PlacementError
from gateforge.placement.physical import (
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
from gateforge.providers.factorio.common import (
    FACTORIO_ARITHMETIC_COMBINATOR,
    FACTORIO_LAMP,
    FACTORIO_PROVIDER,
)
from gateforge.target import ProviderConfiguration


_ARITHMETIC_BOUNDS = PhysicalBounds.centered(2.0, 1.0)
_LAMP_BOUNDS = PhysicalBounds.centered(1.0, 1.0)
_TERMINAL_BOUNDS = PhysicalBounds.centered(1.0, 1.0)
_ARITHMETIC_PORTS = {"a": 0, "b": 1, "y": 2}


def elaborate_factorio_physical_design(
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
    if len(topology.containers) != 1:
        raise PlacementError(
            "Factorio physical elaboration currently requires flat hierarchy"
        )
    topology_container = topology.containers[0]
    components = []
    prefabs = []
    component_ids: dict[MaterialSubject, str] = {}

    objects = {item.identifier: item for item in graph.design.objects}
    for subject in topology_container.subjects:
        if isinstance(subject, ObjectSubject):
            material_object = objects[subject.object]
            if material_object.type == FACTORIO_ARITHMETIC_COMBINATOR:
                kind = "arithmetic-combinator"
                bounds = _ARITHMETIC_BOUNDS
            elif material_object.type == FACTORIO_LAMP:
                kind = "small-lamp"
                bounds = _LAMP_BOUNDS
            else:
                raise PlacementError(
                    f"Unsupported Factorio material object {material_object.type}"
                )
            identifier = f"factorio:object:{subject.object.value}"
            component = PhysicalComponent(
                identifier=identifier,
                provider=FACTORIO_PROVIDER,
                kind=kind,
                source=subject,
                bounds=bounds,
                payload=material_object.configuration,
            )
        elif isinstance(subject, ModuleValueSubject):
            identifier = _terminal_id(subject)
            component = PhysicalComponent(
                identifier=identifier,
                provider=FACTORIO_PROVIDER,
                kind="medium-electric-pole",
                source=subject,
                bounds=_TERMINAL_BOUNDS,
                payload=ProviderConfiguration.from_canonical_data(
                    {
                        "module": subject.module,
                        "port": subject.port,
                        "bits": list(subject.bits),
                        "direction": subject.direction.value,
                    }
                ),
            )
        elif isinstance(subject, ConstantSubject):
            raise PlacementError(
                "Factorio packed constants require routed input-driver support"
            )
        else:
            raise PlacementError(
                f"Unsupported Factorio physical subject {subject!r}"
            )
        component_ids[subject] = identifier
        components.append(component)
        prefabs.append(
            PhysicalPrefabInstance(
                identifier=f"prefab:{identifier}",
                subject=subject,
                bounds=component.bounds,
                components=(PhysicalMember(identifier),),
            )
        )

    connections = tuple(
        PhysicalConnection(
            dependency.net,
            _physical_endpoint(dependency.source, component_ids),
            _physical_endpoint(dependency.target, component_ids),
        )
        for dependency in topology_container.dependencies
    )
    container = PhysicalContainer(
        path=topology_container.path,
        kind=topology_container.kind,
        name=topology_container.name,
        parent=topology_container.parent,
        children=topology_container.children,
        components=tuple(components),
        annotations=(),
        prefabs=tuple(prefabs),
        boundary_ports=(),
        connections=connections,
    )
    return PhysicalDesign(
        material_digest=graph.design.get_digest(),
        target=FACTORIO_PROVIDER,
        root=topology.root,
        containers=(container,),
        topology=topology,
        layout_rules=PhysicalLayoutRules(
            board_grid=1.0,
            board_margin_x=1.0,
            board_margin_y=1.0,
        ),
    )


def _physical_endpoint(
    endpoint: LocalDependencyEndpoint,
    component_ids: Mapping[MaterialSubject, str],
) -> PhysicalEndpoint:
    attachment = endpoint.attachment
    if attachment is None:
        raise PlacementError(
            f"Factorio physical dependency lacks an attachment: {endpoint.subject!r}"
        )
    try:
        identifier = component_ids[endpoint.subject]
    except KeyError as error:
        raise PlacementError(
            f"Factorio physical dependency references unknown subject "
            f"{endpoint.subject!r}"
        ) from error
    return PhysicalEndpoint(
        PhysicalEndpointKind.COMPONENT,
        identifier,
        _connector_index(attachment),
    )


def _connector_index(attachment: MaterialAttachment) -> int:
    if isinstance(attachment, MaterialModuleValueRef):
        return 0
    if isinstance(attachment, MaterialObjectPortRef):
        if attachment.port == "in":
            return 0
        try:
            return _ARITHMETIC_PORTS[attachment.port]
        except KeyError as error:
            raise PlacementError(
                f"Unknown Factorio arithmetic port {attachment.port!r}"
            ) from error
    raise PlacementError(f"Unsupported Factorio attachment {attachment!r}")


def _terminal_id(subject: ModuleValueSubject) -> str:
    return (
        f"factorio:terminal:{subject.module}:{subject.port}:"
        f"{subject.direction.value}"
    )