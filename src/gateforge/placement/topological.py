from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
import heapq
import math

from gateforge.graph import (
    ConstantSubject,
    MaterialGraph,
    MaterialSubject,
    ModulePortSubject,
    ObjectSubject,
    material_subject_key,
)
from gateforge.placement.model import (
    ConstantPlacement,
    FlatPlacementProposal,
    ModulePortPlacement,
    ObjectPlacement,
    PlacementError,
    PlacementOptions,
    PlacementProposal,
    PlacementProvenance,
    Placer,
    ResolvedPlacements,
)
from gateforge.provider import TargetProvider
from gateforge.target import PortDirection


@dataclass(frozen=True, slots=True)
class TopologicalPlacementOptions:
    column_pitch: float = 210.0
    row_pitch: float = 105.0
    routing_group_size: int = 5
    routing_gap_rows: int = 1

    def __post_init__(self) -> None:
        for attribute in ("column_pitch", "row_pitch"):
            value = getattr(self, attribute)
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise PlacementError(f"{attribute} must be numeric")
            normalized = float(value)
            if not math.isfinite(normalized) or normalized <= 0:
                raise PlacementError(f"{attribute} must be finite and positive")
            object.__setattr__(self, attribute, normalized)
        if (
            not isinstance(self.routing_group_size, int)
            or isinstance(self.routing_group_size, bool)
            or self.routing_group_size <= 0
        ):
            raise PlacementError("routing_group_size must be a positive integer")
        if (
            not isinstance(self.routing_gap_rows, int)
            or isinstance(self.routing_gap_rows, bool)
            or self.routing_gap_rows < 0
        ):
            raise PlacementError("routing_gap_rows must be a nonnegative integer")


class TopologicalPlacer(Placer):
    name = "topological"
    version = 1

    def __init__(
        self,
        options: TopologicalPlacementOptions = TopologicalPlacementOptions(),
        providers: Mapping[str, TargetProvider] | None = None,
    ) -> None:
        self.options = options
        self.providers = providers or {}

    def place(self, graph: MaterialGraph) -> PlacementProposal:
        if not graph.subjects:
            raise PlacementError("Topological placer requires at least one subject")
        cycles = _cyclic_components(graph)
        if cycles:
            details = "; ".join(
                ", ".join(repr(subject) for subject in component)
                for component in cycles
            )
            raise PlacementError(
                f"Topological placer requires an acyclic dependency graph; "
                f"cyclic components: {details}"
            )

        columns = _assign_columns(graph)
        coordinates = _assign_coordinates(
            graph,
            columns,
            self.options,
            self.providers,
        )
        _validate_dependency_order(graph, coordinates)
        resolved = _resolved_placements(coordinates)
        return FlatPlacementProposal(
            source_digest=graph.design.get_digest(),
            source_provenance=PlacementProvenance(
                self.name,
                self.version,
                PlacementOptions.from_canonical_data(
                    {
                        "column_pitch": self.options.column_pitch,
                        "row_pitch": self.options.row_pitch,
                        "routing_group_size": self.options.routing_group_size,
                        "routing_gap_rows": self.options.routing_gap_rows,
                    }
                ),
            ),
            resolved=resolved,
        )


def _cyclic_components(
    graph: MaterialGraph,
) -> tuple[tuple[MaterialSubject, ...], ...]:
    visited: set[MaterialSubject] = set()
    finished: list[MaterialSubject] = []
    for root in graph.subjects:
        if root in visited:
            continue
        stack: list[tuple[MaterialSubject, bool]] = [(root, False)]
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
                sorted(graph.successors[subject], key=material_subject_key)
            ):
                if successor not in visited:
                    stack.append((successor, False))

    assigned: set[MaterialSubject] = set()
    cyclic: list[tuple[MaterialSubject, ...]] = []
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
                graph.predecessors[subject],
                key=material_subject_key,
                reverse=True,
            ):
                if predecessor not in assigned:
                    assigned.add(predecessor)
                    stack.append(predecessor)
        if len(component) > 1 or root in graph.successors[root]:
            cyclic.append(tuple(sorted(component, key=material_subject_key)))
    return tuple(sorted(cyclic, key=lambda item: material_subject_key(item[0])))


def _assign_columns(graph: MaterialGraph) -> dict[MaterialSubject, float]:
    object_subjects = tuple(
        subject for subject in graph.subjects if isinstance(subject, ObjectSubject)
    )
    object_set = frozenset(object_subjects)
    object_predecessors = {
        subject: graph.predecessors[subject] & object_set
        for subject in object_subjects
    }
    object_successors = {
        subject: graph.successors[subject] & object_set
        for subject in object_subjects
    }

    reverse_depth: dict[ObjectSubject, int] = {}
    if object_subjects:
        indegree = {
            subject: len(object_predecessors[subject]) for subject in object_subjects
        }
        ready = [
            (material_subject_key(subject), subject)
            for subject in object_subjects
            if indegree[subject] == 0
        ]
        heapq.heapify(ready)
        ordered: list[ObjectSubject] = []
        while ready:
            _, subject = heapq.heappop(ready)
            ordered.append(subject)
            for successor in object_successors[subject]:
                indegree[successor] -= 1
                if indegree[successor] == 0:
                    heapq.heappush(
                        ready,
                        (material_subject_key(successor), successor),
                    )
        if len(ordered) != len(object_subjects):
            raise PlacementError("Object dependency graph is cyclic")
        for subject in reversed(ordered):
            successors = object_successors[subject]
            reverse_depth[subject] = (
                0
                if not successors
                else 1 + max(reverse_depth[item] for item in successors)
            )

    object_span = max(reverse_depth.values(), default=0)
    columns: dict[MaterialSubject, float] = {}
    for subject in object_subjects:
        predecessors = object_predecessors[subject]
        successors = object_successors[subject]
        if not predecessors and not successors:
            column = object_span / 2
        elif not predecessors:
            column = 0.0
        elif not successors:
            column = float(object_span)
        else:
            column = float(object_span - reverse_depth[subject])
        columns[subject] = column

    left_terminals: list[MaterialSubject] = []
    right_terminals: list[MaterialSubject] = []
    for subject in graph.subjects:
        if isinstance(subject, ObjectSubject):
            continue
        if isinstance(subject, ConstantSubject):
            left_terminals.append(subject)
        elif subject.direction == PortDirection.INPUT:
            left_terminals.append(subject)
        elif subject.direction == PortDirection.OUTPUT:
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
    graph: MaterialGraph,
    columns: dict[MaterialSubject, float],
    options: TopologicalPlacementOptions,
    providers: Mapping[str, TargetProvider],
) -> dict[MaterialSubject, tuple[float, float]]:
    grouped: dict[float, list[MaterialSubject]] = {}
    for subject, column in columns.items():
        grouped.setdefault(column, []).append(subject)

    ordered_objects: dict[float, list[ObjectSubject]] = {}
    object_spans: dict[float, list[int]] = {}
    terminal_counts: dict[float, int] = {}
    for column in sorted(grouped):
        objects = [
            subject
            for subject in grouped[column]
            if isinstance(subject, ObjectSubject)
        ]
        objects.sort(key=lambda subject: _object_order_key(graph, subject))
        ordered_objects[column] = objects
        object_spans[column] = [
            _object_row_span(graph, subject, options, providers)
            for subject in objects
        ]
        terminal_counts[column] = sum(
            not isinstance(subject, ObjectSubject) for subject in grouped[column]
        )

    content_height = _content_height(
        [*object_spans.values()],
        [[1] * count for count in terminal_counts.values()],
        routing_group_size=options.routing_group_size,
    )

    object_y: dict[ObjectSubject, float] = {}
    coordinates: dict[MaterialSubject, tuple[float, float]] = {}
    for column in sorted(grouped):
        objects = ordered_objects[column]
        centers, _ = _pack_row_spans(
            object_spans[column],
            content_height,
            options.routing_group_size,
            options.routing_gap_rows,
        )
        for subject, center in zip(objects, centers):
            y = center * options.row_pitch
            object_y[subject] = y
            coordinates[subject] = (column * options.column_pitch, y)

    for column in sorted(grouped):
        terminals = [
            subject
            for subject in grouped[column]
            if not isinstance(subject, ObjectSubject)
        ]
        terminals.sort(key=lambda subject: _terminal_order_key(graph, subject, object_y))
        centers, _ = _pack_row_spans(
            [1] * len(terminals),
            content_height,
            options.routing_group_size,
            options.routing_gap_rows,
        )
        for subject, center in zip(terminals, centers):
            y = center * options.row_pitch
            coordinates[subject] = (column * options.column_pitch, y)

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


def _object_row_span(
    graph: MaterialGraph,
    subject: ObjectSubject,
    options: TopologicalPlacementOptions,
    providers: Mapping[str, TargetProvider],
) -> int:
    material_object = next(
        item for item in graph.design.objects if item.identifier == subject.object
    )
    provider = providers.get(material_object.type.provider)
    if provider is None:
        return 1
    geometry = provider.object_placement_geometry(material_object)
    if geometry is None:
        return 1
    return max(1, math.ceil(geometry.height / options.row_pitch - 1e-12))


def _content_height(
    *span_groups: list[list[int]],
    routing_group_size: int,
) -> int:
    spans_by_column = [spans for group in span_groups for spans in group]
    height = max((sum(spans) for spans in spans_by_column), default=1)
    while True:
        required = max(
            (
                _pack_row_spans(
                    spans,
                    height,
                    routing_group_size,
                    0,
                )[1]
                for spans in spans_by_column
            ),
            default=height,
        )
        if required <= height:
            return height
        height = required


def _pack_row_spans(
    spans: list[int],
    content_height: int,
    routing_group_size: int,
    routing_gap_rows: int,
) -> tuple[list[float], int]:
    if not spans:
        return [], 0
    cursor = max(0, (content_height - sum(spans)) // 2)
    centers: list[float] = []
    for span in spans:
        if span <= routing_group_size:
            offset = cursor % routing_group_size
            if offset + span > routing_group_size:
                cursor += routing_group_size - offset
        start = cursor
        end = cursor + span - 1
        centers.append(
            (
                _physical_row(start, routing_group_size, routing_gap_rows)
                + _physical_row(end, routing_group_size, routing_gap_rows)
            )
            / 2
        )
        cursor += span
    return centers, cursor


def _physical_row(
    content_row: int,
    routing_group_size: int,
    routing_gap_rows: int,
) -> int:
    return content_row + (content_row // routing_group_size) * routing_gap_rows


def _object_order_key(
    graph: MaterialGraph,
    subject: ObjectSubject,
) -> tuple[str, str, str, str]:
    material_object = next(
        item for item in graph.design.objects if item.identifier == subject.object
    )
    return (
        material_object.hierarchy,
        material_object.occurrence.value,
        material_object.role,
        material_object.identifier.value,
    )


def _terminal_order_key(
    graph: MaterialGraph,
    subject: MaterialSubject,
    object_y: dict[ObjectSubject, float],
) -> tuple[object, ...]:
    adjacent_objects = [
        item
        for item in graph.predecessors[subject] | graph.successors[subject]
        if isinstance(item, ObjectSubject) and item in object_y
    ]
    if adjacent_objects:
        mean_y = sum(object_y[item] for item in adjacent_objects) / len(adjacent_objects)
        return (0, mean_y, material_subject_key(subject))
    net_ids = tuple(sorted(item.value for item in graph.incident_nets[subject]))
    return (1, net_ids, material_subject_key(subject))


def _validate_dependency_order(
    graph: MaterialGraph,
    coordinates: dict[MaterialSubject, tuple[float, float]],
) -> None:
    for source, successors in graph.successors.items():
        for target in successors:
            if coordinates[source][0] >= coordinates[target][0]:
                raise PlacementError(
                    f"Topological columns do not place {source} before {target}"
                )


def _resolved_placements(
    coordinates: dict[MaterialSubject, tuple[float, float]],
) -> ResolvedPlacements:
    objects: list[ObjectPlacement] = []
    module_ports: list[ModulePortPlacement] = []
    constants: list[ConstantPlacement] = []
    for subject, (x, y) in coordinates.items():
        if isinstance(subject, ObjectSubject):
            objects.append(ObjectPlacement(subject.object, x, y, 0.0))
        elif isinstance(subject, ModulePortSubject):
            module_ports.append(
                ModulePortPlacement(
                    subject.module,
                    subject.port,
                    subject.bit,
                    subject.direction,
                    x,
                    y,
                    0.0,
                )
            )
        else:
            constants.append(ConstantPlacement(subject.net, subject.value, x, y, 0.0))
    return ResolvedPlacements(tuple(objects), tuple(module_ports), tuple(constants))
