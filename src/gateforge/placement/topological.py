from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
import heapq
import math
from typing import Protocol

from gateforge.graph import (
    ConstantSubject,
    MaterialGraph,
    MaterialSubject,
    ModulePortSubject,
    ModuleValueSubject,
    ObjectSubject,
    material_subject_key,
)
from gateforge.hierarchy import (
    GeneratedHierarchyPolicy,
    PhysicalHierarchyPolicy,
)
from gateforge.material import MaterialNetId
from gateforge.placement.hierarchy import (
    BoundaryPortSubject,
    ChildContainerSubject,
    LocalPlacementSubject,
    PlacementContainerTopology,
    build_placement_hierarchy,
    local_subject_key,
)
from gateforge.placement.model import (
    AnnotationPlacement,
    BoundaryPortPlacement,
    ComponentPlacement,
    PhysicalBounds,
    PlacedContainer,
    PlacedDesign,
    PrefabPlacement,
    PlacementError,
    PlacementOptions,
    PlacementProposal,
    PlacementProvenance,
    Placer,
    ResolvedPlacementProposal,
)
from gateforge.placement.physical import (
    PhysicalContainer,
    PhysicalDesign,
    PhysicalLayoutRules,
)
from gateforge.provider import TargetProvider
from gateforge.target import PortDirection, ProviderConfiguration


@dataclass(frozen=True, slots=True)
class TopologicalPlacementOptions:
    column_pitch: float = 210.0
    row_pitch: float = 52.5
    routing_group_height: float = 250.0
    routing_gap_rows: int = 1

    def __post_init__(self) -> None:
        for attribute in ("column_pitch", "row_pitch", "routing_group_height"):
            value = getattr(self, attribute)
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise PlacementError(f"{attribute} must be numeric")
            normalized = float(value)
            if not math.isfinite(normalized) or normalized <= 0:
                raise PlacementError(f"{attribute} must be finite and positive")
            object.__setattr__(self, attribute, normalized)
        if (
            not isinstance(self.routing_gap_rows, int)
            or isinstance(self.routing_gap_rows, bool)
            or self.routing_gap_rows < 0
        ):
            raise PlacementError("routing_gap_rows must be a nonnegative integer")


class _LocalGraph(Protocol):
    subjects: tuple[LocalPlacementSubject, ...]
    predecessors: Mapping[
        LocalPlacementSubject,
        frozenset[LocalPlacementSubject],
    ]
    successors: Mapping[
        LocalPlacementSubject,
        frozenset[LocalPlacementSubject],
    ]
    incident_nets: Mapping[LocalPlacementSubject, frozenset[MaterialNetId]]


@dataclass(frozen=True, slots=True)
class _LocalLayout:
    topology: PlacementContainerTopology
    coordinates: Mapping[LocalPlacementSubject, tuple[float, float]]
    board: PhysicalBounds
    facade: PhysicalBounds
    components: tuple[ComponentPlacement, ...]
    prefabs: tuple[PrefabPlacement, ...]
    boundary_ports: tuple[BoundaryPortPlacement, ...]
    annotations: tuple[AnnotationPlacement, ...]


type _ObjectLikeSubject = ObjectSubject | ChildContainerSubject


class TopologicalPlacer(Placer):
    name = "topological"
    version = 1

    def __init__(
        self,
        options: TopologicalPlacementOptions = TopologicalPlacementOptions(),
        providers: Mapping[str, TargetProvider] | None = None,
        physical_hierarchy: PhysicalHierarchyPolicy = PhysicalHierarchyPolicy(),
        generated_hierarchy: GeneratedHierarchyPolicy = GeneratedHierarchyPolicy(),
    ) -> None:
        self.options = options
        self.providers = providers or {}
        self.physical_hierarchy = physical_hierarchy
        self.generated_hierarchy = generated_hierarchy

    def place(self, graph: MaterialGraph) -> PlacementProposal:
        if not graph.subjects:
            raise PlacementError("Topological placer requires at least one subject")
        target = _target_identifier(graph)
        physical = _elaborate_physical_design(
            graph,
            target,
            self.providers,
            self.physical_hierarchy,
            self.generated_hierarchy,
        )
        hierarchy = (
            physical.topology
            if physical is not None
            else build_placement_hierarchy(
                graph,
                self.providers,
                self.physical_hierarchy,
                self.generated_hierarchy,
            )
        )
        physical_by_path = (
            {}
            if physical is None
            else {item.path: item for item in physical.containers}
        )
        layout_rules = (
            PhysicalLayoutRules() if physical is None else physical.layout_rules
        )
        by_path = {item.path: item for item in hierarchy.containers}
        layouts: dict[str, _LocalLayout] = {}
        for topology in sorted(
            hierarchy.containers,
            key=lambda item: (-_container_depth(item.path, by_path), item.path),
        ):
            subject_bounds = {
                subject: _subject_bounds(
                    graph,
                    subject,
                    layouts,
                    physical_by_path.get(topology.path),
                    self.options,
                    self.providers,
                )
                for subject in topology.subjects
            }
            components = _object_components(topology)
            columns = _assign_columns(topology, components)
            coordinates = _assign_coordinates(
                topology,
                graph,
                columns,
                subject_bounds,
                self.options,
            )
            _validate_dependency_order(topology, coordinates, components)
            boundary_ports = _resolved_boundary_ports(
                topology,
                coordinates,
            )
            components, prefabs, annotations = _resolved_physical_entities(
                physical_by_path.get(topology.path),
                target,
                subject_bounds,
                coordinates,
            )
            board = _content_bounds(
                coordinates,
                subject_bounds,
                self.options,
                layout_rules,
            )
            facade = _facade_bounds(topology, self.options, layout_rules)
            layouts[topology.path] = _LocalLayout(
                topology,
                coordinates,
                board,
                facade,
                components,
                prefabs,
                boundary_ports,
                annotations,
            )

        child_positions = {
            subject.path: layout.coordinates[subject]
            for layout in layouts.values()
            for subject in layout.topology.subjects
            if isinstance(subject, ChildContainerSubject)
        }
        containers = tuple(
            PlacedContainer(
                path=layout.topology.path,
                kind=layout.topology.kind,
                name=layout.topology.name,
                parent=layout.topology.parent,
                x=0.0 if layout.topology.parent is None else child_positions[layout.topology.path][0],
                y=0.0 if layout.topology.parent is None else child_positions[layout.topology.path][1],
                angle=0.0,
                board=layout.board,
                facade=layout.facade,
                components=layout.components,
                boundary_ports=layout.boundary_ports,
                annotations=layout.annotations,
                prefabs=layout.prefabs,
                connections=(
                    ()
                    if layout.topology.path not in physical_by_path
                    else physical_by_path[layout.topology.path].connections
                ),
                children=layout.topology.children,
            )
            for layout in layouts.values()
        )
        placed = PlacedDesign(
            material_digest=graph.design.get_digest(),
            target=target,
            provenance=PlacementProvenance(
                self.name,
                self.version,
                PlacementOptions.from_canonical_data(
                    {
                        "column_pitch": self.options.column_pitch,
                        "row_pitch": self.options.row_pitch,
                        "routing_group_height": self.options.routing_group_height,
                        "routing_gap_rows": self.options.routing_gap_rows,
                        "physical_hierarchy": self.physical_hierarchy.mode.value,
                        "hierarchy_threshold": self.physical_hierarchy.threshold,
                        "generated_hierarchy": self.generated_hierarchy.mode.value,
                        "generated_auto_min_objects": (
                            self.generated_hierarchy.auto_min_objects
                        ),
                    }
                ),
            ),
            root=hierarchy.root,
            containers=containers,
        )
        return ResolvedPlacementProposal(placed)


def _object_components(
    graph: _LocalGraph,
) -> tuple[tuple[_ObjectLikeSubject, ...], ...]:
    object_subjects = tuple(
        subject for subject in graph.subjects if _is_object_like(subject)
    )
    object_set = frozenset(object_subjects)
    visited: set[_ObjectLikeSubject] = set()
    finished: list[_ObjectLikeSubject] = []
    for root in object_subjects:
        if root in visited:
            continue
        stack: list[tuple[_ObjectLikeSubject, bool]] = [(root, False)]
        while stack:
            subject, expanded = stack.pop()
            if expanded:
                finished.append(subject)
                continue
            if subject in visited:
                continue
            visited.add(subject)
            stack.append((subject, True))
            for successor in reversed(
                sorted(
                    graph.successors[subject] & object_set,
                    key=local_subject_key,
                )
            ):
                if successor not in visited:
                    stack.append((successor, False))

    assigned: set[_ObjectLikeSubject] = set()
    components: list[tuple[_ObjectLikeSubject, ...]] = []
    for root in reversed(finished):
        if root in assigned:
            continue
        component: set[MaterialSubject] = set()
        stack = [root]
        assigned.add(root)
        while stack:
            subject = stack.pop()
            component.add(subject)
            for predecessor in sorted(
                graph.predecessors[subject] & object_set,
                key=local_subject_key,
                reverse=True,
            ):
                if predecessor not in assigned:
                    assigned.add(predecessor)
                    stack.append(predecessor)
        components.append(tuple(sorted(component, key=local_subject_key)))
    return tuple(
        sorted(components, key=lambda item: local_subject_key(item[0]))
    )


def _component_layers(
    graph: _LocalGraph,
    subjects: tuple[_ObjectLikeSubject, ...],
) -> dict[_ObjectLikeSubject, int]:
    if len(subjects) <= 1:
        return {subject: 0 for subject in subjects}
    subject_set = frozenset(subjects)
    order = _cycle_breaking_order(graph, subject_set)
    order_index = {subject: index for index, subject in enumerate(order)}
    layers: dict[_ObjectLikeSubject, int] = {}
    for subject in order:
        layers[subject] = max(
            (
                layers[predecessor] + 1
                for predecessor in graph.predecessors[subject] & subject_set
                if order_index[predecessor] < order_index[subject]
            ),
            default=0,
        )
    return layers


def _cycle_breaking_order(
    graph: _LocalGraph,
    subjects: frozenset[_ObjectLikeSubject],
) -> tuple[_ObjectLikeSubject, ...]:
    successors = {
        subject: (graph.successors[subject] & subjects) - {subject}
        for subject in subjects
    }
    predecessors = {
        subject: (graph.predecessors[subject] & subjects) - {subject}
        for subject in subjects
    }
    remaining = set(subjects)
    left: list[_ObjectLikeSubject] = []
    right: list[_ObjectLikeSubject] = []
    while remaining:
        sinks = [
            subject
            for subject in remaining
            if not successors[subject] & remaining
        ]
        if sinks:
            subject = min(sinks, key=local_subject_key)
            right.append(subject)
            remaining.remove(subject)
            continue
        sources = [
            subject
            for subject in remaining
            if not predecessors[subject] & remaining
        ]
        if sources:
            subject = min(sources, key=local_subject_key)
            left.append(subject)
            remaining.remove(subject)
            continue
        subject = min(
            remaining,
            key=lambda item: (
                -(
                    len(successors[item] & remaining)
                    - len(predecessors[item] & remaining)
                ),
                local_subject_key(item),
            ),
        )
        left.append(subject)
        remaining.remove(subject)
    return tuple((*left, *reversed(right)))


def _assign_columns(
    graph: _LocalGraph,
    components: tuple[tuple[_ObjectLikeSubject, ...], ...],
) -> dict[LocalPlacementSubject, float]:
    object_subjects = tuple(
        subject for subject in graph.subjects if _is_object_like(subject)
    )
    object_set = frozenset(object_subjects)
    component_by_subject = {
        subject: index
        for index, component in enumerate(components)
        for subject in component
    }
    component_predecessors: dict[int, set[int]] = {
        index: set() for index in range(len(components))
    }
    component_successors: dict[int, set[int]] = {
        index: set() for index in range(len(components))
    }
    for subject in object_subjects:
        source_component = component_by_subject[subject]
        for successor in graph.successors[subject] & object_set:
            target_component = component_by_subject[successor]
            if source_component == target_component:
                continue
            component_successors[source_component].add(target_component)
            component_predecessors[target_component].add(source_component)

    ordered: list[int] = []
    if components:
        indegree = {
            index: len(component_predecessors[index])
            for index in range(len(components))
        }
        ready = [
            (local_subject_key(components[index][0]), index)
            for index in range(len(components))
            if indegree[index] == 0
        ]
        heapq.heapify(ready)
        while ready:
            _, component = heapq.heappop(ready)
            ordered.append(component)
            for successor in component_successors[component]:
                indegree[successor] -= 1
                if indegree[successor] == 0:
                    heapq.heappush(
                        ready,
                        (local_subject_key(components[successor][0]), successor),
                    )
        if len(ordered) != len(components):
            raise AssertionError("SCC condensation graph is cyclic")

    component_layers = {
        index: _component_layers(graph, component)
        for index, component in enumerate(components)
    }
    component_spans = {
        index: max(layers.values(), default=0)
        for index, layers in component_layers.items()
    }
    reverse_extent: dict[int, int] = {}
    for component in reversed(ordered):
        successors = component_successors[component]
        reverse_extent[component] = component_spans[component] + (
            0
            if not successors
            else 1 + max(reverse_extent[item] for item in successors)
        )

    object_span = max(reverse_extent.values(), default=0)
    columns: dict[LocalPlacementSubject, float] = {}
    for component, subjects in enumerate(components):
        predecessors = component_predecessors[component]
        successors = component_successors[component]
        if not predecessors and not successors:
            component_start = (object_span - component_spans[component]) / 2
        elif not predecessors:
            component_start = 0.0
        elif not successors:
            component_start = float(object_span - component_spans[component])
        else:
            component_start = float(object_span - reverse_extent[component])
        for subject in subjects:
            columns[subject] = component_start + component_layers[component][subject]

    left_terminals: list[LocalPlacementSubject] = []
    right_terminals: list[LocalPlacementSubject] = []
    for subject in graph.subjects:
        if _is_object_like(subject):
            continue
        if isinstance(subject, ConstantSubject):
            left_terminals.append(subject)
        elif isinstance(
            subject,
            (ModulePortSubject, ModuleValueSubject, BoundaryPortSubject),
        ) and (
            subject.direction == PortDirection.INPUT
        ):
            left_terminals.append(subject)
        elif isinstance(
            subject,
            (ModulePortSubject, ModuleValueSubject, BoundaryPortSubject),
        ) and (
            subject.direction == PortDirection.OUTPUT
        ):
            right_terminals.append(subject)
        else:
            predecessors = graph.predecessors[subject]
            successors = graph.successors[subject]
            if successors and not predecessors:
                left_terminals.append(subject)
            elif predecessors and not successors:
                right_terminals.append(subject)
            else:
                raise PlacementError(
                    f"Topological placer cannot classify INOUT terminal {subject}"
                )

    if object_subjects:
        left_column = -1.0
        right_column = float(object_span + 1)
    elif left_terminals and right_terminals:
        left_column = 0.0
        right_column = 1.0
    else:
        left_column = right_column = 0.0
    for subject in left_terminals:
        columns[subject] = left_column
    for subject in right_terminals:
        columns[subject] = right_column
    return columns


def _assign_coordinates(
    graph: _LocalGraph,
    material_graph: MaterialGraph,
    columns: dict[LocalPlacementSubject, float],
    subject_bounds: Mapping[LocalPlacementSubject, PhysicalBounds],
    options: TopologicalPlacementOptions,
) -> dict[LocalPlacementSubject, tuple[float, float]]:
    if not columns:
        return {}
    grouped: dict[float, list[LocalPlacementSubject]] = {}
    for subject, column in columns.items():
        grouped.setdefault(column, []).append(subject)

    ordered_objects: dict[float, list[_ObjectLikeSubject]] = {}
    object_heights: dict[float, list[float]] = {}
    terminal_heights: dict[float, list[float]] = {}
    for column in sorted(grouped):
        objects = [
            subject
            for subject in grouped[column]
            if _is_object_like(subject)
        ]
        objects.sort(
            key=lambda subject: _object_order_key(material_graph, subject)
        )
        ordered_objects[column] = objects
        object_heights[column] = [
            max(options.row_pitch, subject_bounds[subject].height)
            for subject in objects
        ]
        terminal_heights[column] = [
            max(options.row_pitch, subject_bounds[subject].height)
            for subject in grouped[column]
            if not _is_object_like(subject)
        ]

    content_height = _content_height(
        [*object_heights.values()],
        [*terminal_heights.values()],
        routing_group_height=options.routing_group_height,
    )
    routing_gap_height = options.routing_gap_rows * options.row_pitch

    object_y: dict[_ObjectLikeSubject, float] = {}
    coordinates: dict[LocalPlacementSubject, tuple[float, float]] = {}
    for column in sorted(grouped):
        objects = ordered_objects[column]
        centers, _ = _pack_heights(
            object_heights[column],
            content_height,
            options.routing_group_height,
            routing_gap_height,
        )
        for subject, center in zip(objects, centers):
            object_y[subject] = center
            coordinates[subject] = (column * options.column_pitch, center)

    for column in sorted(grouped):
        terminals = [
            subject
            for subject in grouped[column]
            if not _is_object_like(subject)
        ]
        terminals.sort(key=lambda subject: _terminal_order_key(graph, subject, object_y))
        centers, _ = _pack_heights(
            [max(options.row_pitch, subject_bounds[item].height) for item in terminals],
            content_height,
            options.routing_group_height,
            routing_gap_height,
        )
        for subject, center in zip(terminals, centers):
            coordinates[subject] = (column * options.column_pitch, center)

    min_x = min(x for x, _ in coordinates.values())
    max_x = max(x for x, _ in coordinates.values())
    min_y = min(y for _, y in coordinates.values())
    max_y = max(y for _, y in coordinates.values())
    x_offset = (min_x + max_x) / 2
    y_offset = (min_y + max_y) / 2
    return {
        subject: (x - x_offset, y - y_offset)
        for subject, (x, y) in coordinates.items()
    }


def _subject_bounds(
    graph: MaterialGraph,
    subject: LocalPlacementSubject,
    layouts: Mapping[str, _LocalLayout],
    physical_container: PhysicalContainer | None,
    options: TopologicalPlacementOptions,
    providers: Mapping[str, TargetProvider],
) -> PhysicalBounds:
    if isinstance(subject, ChildContainerSubject):
        return layouts[subject.path].facade
    if physical_container is not None:
        prefab = next(
            (item for item in physical_container.prefabs if item.subject == subject),
            None,
        )
        if prefab is None and isinstance(subject, BoundaryPortSubject):
            return PhysicalBounds.centered(0.0, options.row_pitch)
        if prefab is None:
            raise PlacementError(
                f"Physical container {physical_container.path!r} has no prefab "
                f"for placement subject {subject!r}"
            )
        assert prefab is not None
        return prefab.bounds
    if not isinstance(subject, ObjectSubject):
        return PhysicalBounds.centered(0.0, options.row_pitch)
    material_object = next(
        item for item in graph.design.objects if item.identifier == subject.object
    )
    provider = providers.get(material_object.type.provider)
    if provider is None:
        return PhysicalBounds.centered(options.row_pitch, options.row_pitch)
    geometry = provider.object_placement_geometry(material_object)
    if geometry is None:
        return PhysicalBounds.centered(options.row_pitch, options.row_pitch)
    return PhysicalBounds.centered(
        geometry.width,
        max(options.row_pitch, geometry.height),
    )


def _content_height(
    *height_groups: list[list[float]],
    routing_group_height: float,
) -> float:
    heights_by_column = [heights for group in height_groups for heights in group]
    height = max(
        (
            _pack_heights(heights, 0.0, routing_group_height, 0.0)[1]
            for heights in heights_by_column
        ),
        default=routing_group_height,
    )
    while True:
        required = max(
            (
                _pack_heights(
                    heights,
                    height,
                    routing_group_height,
                    0.0,
                )[1]
                for heights in heights_by_column
            ),
            default=height,
        )
        if required <= height + 1e-12:
            return height
        height = required


def _pack_heights(
    heights: list[float],
    content_height: float,
    routing_group_height: float,
    routing_gap_height: float,
) -> tuple[list[float], float]:
    if not heights:
        return [], 0.0
    natural_height = _packed_content_height(heights, routing_group_height)
    cursor = max(0.0, (content_height - natural_height) / 2)
    centers: list[float] = []
    for height in heights:
        if height <= routing_group_height:
            offset = cursor % routing_group_height
            if offset + height > routing_group_height + 1e-12:
                cursor += routing_group_height - offset
        start = cursor
        physical_start = _physical_position(
            start,
            routing_group_height,
            routing_gap_height,
        )
        centers.append(physical_start + height / 2)
        cursor += height
    return centers, cursor


def _packed_content_height(
    heights: list[float],
    routing_group_height: float,
) -> float:
    cursor = 0.0
    for height in heights:
        if height <= routing_group_height:
            offset = cursor % routing_group_height
            if offset + height > routing_group_height + 1e-12:
                cursor += routing_group_height - offset
        cursor += height
    return cursor


def _physical_position(
    content_position: float,
    routing_group_height: float,
    routing_gap_height: float,
) -> float:
    completed_groups = math.floor(
        content_position / routing_group_height + 1e-12
    )
    return content_position + completed_groups * routing_gap_height


def _object_order_key(
    graph: MaterialGraph,
    subject: _ObjectLikeSubject,
) -> tuple[object, ...]:
    if isinstance(subject, ChildContainerSubject):
        return (1, subject.path)
    material_object = next(
        item for item in graph.design.objects if item.identifier == subject.object
    )
    return (
        0,
        material_object.hierarchy,
        material_object.occurrence.value,
        material_object.role,
        material_object.identifier.value,
    )


def _terminal_order_key(
    graph: _LocalGraph,
    subject: LocalPlacementSubject,
    object_y: dict[_ObjectLikeSubject, float],
) -> tuple[object, ...]:
    adjacent_objects = [
        item
        for item in graph.predecessors[subject] | graph.successors[subject]
        if _is_object_like(item) and item in object_y
    ]
    if adjacent_objects:
        mean_y = sum(object_y[item] for item in adjacent_objects) / len(adjacent_objects)
        return (0, mean_y, local_subject_key(subject))
    net_ids = tuple(sorted(item.value for item in graph.incident_nets[subject]))
    return (1, net_ids, local_subject_key(subject))


def _validate_dependency_order(
    graph: _LocalGraph,
    coordinates: dict[LocalPlacementSubject, tuple[float, float]],
    components: tuple[tuple[_ObjectLikeSubject, ...], ...],
) -> None:
    component_by_subject = {
        subject: index
        for index, component in enumerate(components)
        for subject in component
    }
    for source, successors in graph.successors.items():
        for target in successors:
            if (
                _is_object_like(source)
                and _is_object_like(target)
                and component_by_subject[source] == component_by_subject[target]
            ):
                continue
            if coordinates[source][0] >= coordinates[target][0]:
                raise PlacementError(
                    f"Topological columns do not place {source} before {target}"
                )


def _resolved_boundary_ports(
    topology: PlacementContainerTopology,
    coordinates: dict[LocalPlacementSubject, tuple[float, float]],
) -> tuple[BoundaryPortPlacement, ...]:
    boundary_ports: list[BoundaryPortPlacement] = []
    boundary_indices = {
        boundary: index
        for direction in (PortDirection.INPUT, PortDirection.OUTPUT)
        for index, boundary in enumerate(
            item
            for item in topology.boundary_ports
            if item.direction == direction
        )
    }
    for subject, (x, y) in coordinates.items():
        if isinstance(subject, BoundaryPortSubject):
            boundary_ports.append(
                BoundaryPortPlacement(
                    subject.net,
                    subject.direction,
                    boundary_indices[subject],
                    x,
                    y,
                    0.0,
                )
            )
    return tuple(boundary_ports)


def _resolved_physical_entities(
    container: PhysicalContainer | None,
    target: str,
    subject_bounds: Mapping[LocalPlacementSubject, PhysicalBounds],
    coordinates: Mapping[LocalPlacementSubject, tuple[float, float]],
) -> tuple[
    tuple[ComponentPlacement, ...],
    tuple[PrefabPlacement, ...],
    tuple[AnnotationPlacement, ...],
]:
    if container is None:
        components = []
        prefabs = []
        for subject, (x, y) in coordinates.items():
            if isinstance(subject, (BoundaryPortSubject, ChildContainerSubject)):
                continue
            identifier = _generic_component_id(subject)
            components.append(
                ComponentPlacement(
                    identifier,
                    target,
                    _generic_component_kind(subject),
                    subject,
                    subject_bounds[subject],
                    ProviderConfiguration(),
                    x,
                    y,
                    0.0,
                )
            )
            prefabs.append(
                PrefabPlacement(
                    f"prefab:{identifier}",
                    subject,
                    subject_bounds[subject],
                    x,
                    y,
                    0.0,
                    (identifier,),
                )
            )
        return tuple(components), tuple(prefabs), ()

    component_by_id = {item.identifier: item for item in container.components}
    annotation_by_id = {item.identifier: item for item in container.annotations}
    components: list[ComponentPlacement] = []
    annotations: list[AnnotationPlacement] = []
    prefabs: list[PrefabPlacement] = []
    for prefab in container.prefabs:
        x, y = coordinates[prefab.subject]
        for member in prefab.components:
            component = component_by_id[member.identifier]
            components.append(
                ComponentPlacement(
                    component.identifier,
                    component.provider,
                    component.kind,
                    component.source,
                    component.bounds,
                    component.payload,
                    x + member.x,
                    y + member.y,
                    member.angle,
                )
            )
        for member in prefab.annotations:
            annotation = annotation_by_id[member.identifier]
            annotations.append(
                AnnotationPlacement(
                    annotation.identifier,
                    annotation.text,
                    x + member.x,
                    y + member.y,
                    member.angle,
                    annotation.scale_x,
                    annotation.scale_y,
                    annotation.provider,
                    annotation.bounds,
                    annotation.payload,
                )
            )
        prefabs.append(
            PrefabPlacement(
                prefab.identifier,
                prefab.subject,
                prefab.bounds,
                x,
                y,
                0.0,
                tuple(item.identifier for item in prefab.components),
                tuple(item.identifier for item in prefab.annotations),
            )
        )
    return tuple(components), tuple(prefabs), tuple(annotations)


def _generic_component_id(subject: MaterialSubject) -> str:
    if isinstance(subject, ObjectSubject):
        return f"object:{subject.object.value}"
    if isinstance(subject, ModulePortSubject):
        return (
            f"module:{subject.module}:{subject.port}:{subject.bit}:"
            f"{subject.direction.value}"
        )
    if isinstance(subject, ModuleValueSubject):
        return (
            f"module-value:{subject.module}:{subject.port}:"
            f"{','.join(str(bit) for bit in subject.bits)}:"
            f"{subject.direction.value}"
        )
    return f"constant:{subject.net.value}:{subject.value}"


def _generic_component_kind(subject: MaterialSubject) -> str:
    if isinstance(subject, ObjectSubject):
        return "material_object"
    if isinstance(subject, ModulePortSubject):
        return "module_port"
    if isinstance(subject, ModuleValueSubject):
        return "module_value"
    return "constant"


def _content_bounds(
    coordinates: Mapping[LocalPlacementSubject, tuple[float, float]],
    subject_bounds: Mapping[LocalPlacementSubject, PhysicalBounds],
    options: TopologicalPlacementOptions,
    rules: PhysicalLayoutRules,
) -> PhysicalBounds:
    if not coordinates:
        content_half_width = options.row_pitch / 2
        content_half_height = options.row_pitch / 2
    else:
        min_x = min(
            x + subject_bounds[subject].min_x
            for subject, (x, _) in coordinates.items()
        )
        max_x = max(
            x + subject_bounds[subject].max_x
            for subject, (x, _) in coordinates.items()
        )
        min_y = min(
            y + subject_bounds[subject].min_y
            for subject, (_, y) in coordinates.items()
        )
        max_y = max(
            y + subject_bounds[subject].max_y
            for subject, (_, y) in coordinates.items()
        )
        content_half_width = max(abs(min_x), abs(max_x))
        content_half_height = max(abs(min_y), abs(max_y))
    half_width = max(
        rules.minimum_board_half_width,
        _snap_up(content_half_width + rules.board_margin_x, rules.board_grid),
    )
    half_height = max(
        rules.minimum_board_half_height,
        _snap_up(content_half_height + rules.board_margin_y, rules.board_grid),
    )
    return PhysicalBounds(-half_width, -half_height, half_width, half_height)


def _facade_bounds(
    topology: PlacementContainerTopology,
    options: TopologicalPlacementOptions,
    rules: PhysicalLayoutRules,
) -> PhysicalBounds:
    input_count = sum(
        item.direction == PortDirection.INPUT for item in topology.boundary_ports
    )
    output_count = sum(
        item.direction == PortDirection.OUTPUT for item in topology.boundary_ports
    )
    return PhysicalBounds.centered(
        rules.facade_width,
        rules.facade_port_pitch
        * max(input_count, output_count, rules.facade_min_ports),
    )


def _snap_up(value: float, grid: float) -> float:
    if grid <= 0:
        return value
    return math.ceil(value / grid - 1e-12) * grid


def _container_depth(
    path: str,
    containers: Mapping[str, PlacementContainerTopology],
) -> int:
    depth = 0
    parent = containers[path].parent
    while parent is not None:
        depth += 1
        parent = containers[parent].parent
    return depth


def _target_identifier(graph: MaterialGraph) -> str:
    providers = {
        *(item.type.provider for item in graph.design.objects),
        *(item.type.provider for item in graph.design.nets),
    }
    if len(providers) != 1:
        raise PlacementError(
            "Hierarchical placement requires exactly one target provider"
        )
    return next(iter(providers))


def _elaborate_physical_design(
    graph: MaterialGraph,
    target: str,
    providers: Mapping[str, TargetProvider],
    physical_hierarchy: PhysicalHierarchyPolicy,
    generated_hierarchy: GeneratedHierarchyPolicy,
) -> PhysicalDesign | None:
    provider = providers.get(target)
    if provider is None or provider.physical_elaborator is None:
        return None
    physical = provider.elaborate_physical_design(
        graph,
        providers,
        physical_hierarchy,
        generated_hierarchy,
    )
    if physical.material_digest != graph.design.get_digest():
        raise PlacementError("Physical design material digest does not match graph")
    if physical.target != target:
        raise PlacementError(
            f"Physical design target {physical.target!r} does not match {target!r}"
        )
    return physical


def _is_object_like(subject: LocalPlacementSubject) -> bool:
    return isinstance(subject, (ObjectSubject, ChildContainerSubject))
