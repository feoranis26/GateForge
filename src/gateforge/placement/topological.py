from __future__ import annotations

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
from gateforge.target import PortDirection


@dataclass(frozen=True, slots=True)
class TopologicalPlacementOptions:
    column_pitch: float = 262.5
    row_pitch: float = 262.5

    def __post_init__(self) -> None:
        for attribute in ("column_pitch", "row_pitch"):
            value = getattr(self, attribute)
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise PlacementError(f"{attribute} must be numeric")
            normalized = float(value)
            if not math.isfinite(normalized) or normalized <= 0:
                raise PlacementError(f"{attribute} must be finite and positive")
            object.__setattr__(self, attribute, normalized)


class TopologicalPlacer(Placer):
    name = "topological"
    version = 1

    def __init__(
        self,
        options: TopologicalPlacementOptions = TopologicalPlacementOptions(),
    ) -> None:
        self.options = options

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
        coordinates = _assign_coordinates(graph, columns, self.options)
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
) -> dict[MaterialSubject, tuple[float, float]]:
    grouped: dict[float, list[MaterialSubject]] = {}
    for subject, column in columns.items():
        grouped.setdefault(column, []).append(subject)

    object_y: dict[ObjectSubject, float] = {}
    coordinates: dict[MaterialSubject, tuple[float, float]] = {}
    for column in sorted(grouped):
        objects = [
            subject
            for subject in grouped[column]
            if isinstance(subject, ObjectSubject)
        ]
        objects.sort(key=lambda subject: _object_order_key(graph, subject))
        for index, subject in enumerate(objects):
            y = (index - (len(objects) - 1) / 2) * options.row_pitch
            object_y[subject] = y
            coordinates[subject] = (column * options.column_pitch, y)

    for column in sorted(grouped):
        terminals = [
            subject
            for subject in grouped[column]
            if not isinstance(subject, ObjectSubject)
        ]
        terminals.sort(key=lambda subject: _terminal_order_key(graph, subject, object_y))
        for index, subject in enumerate(terminals):
            y = (index - (len(terminals) - 1) / 2) * options.row_pitch
            coordinates[subject] = (column * options.column_pitch, y)

    min_x = min(x for x, _ in coordinates.values())
    max_x = max(x for x, _ in coordinates.values())
    offset = (min_x + max_x) / 2
    return {subject: (x - offset, y) for subject, (x, y) in coordinates.items()}


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
