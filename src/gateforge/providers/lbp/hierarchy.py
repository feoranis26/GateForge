from __future__ import annotations

from collections import defaultdict
from collections.abc import Mapping
from dataclasses import replace
import math

from gateforge.graph import MaterialGraph
from gateforge.hierarchy import (
    PhysicalHierarchyMode,
    PhysicalHierarchyPolicy,
    nearest_retained_parent,
    retained_module_paths,
)
from gateforge.material import (
    MaterialConstantRef,
    MaterialDesign,
    MaterialModuleOccurrence,
    MaterialNetId,
    MaterialObjectId,
    MaterialObjectPortRef,
)
from gateforge.placement import PlacedDesign
from gateforge.provider import TargetProvider
from gateforge.providers.lbp.plan import (
    LbpBoardSize,
    LbpContainer,
    LbpGadgetId,
    LbpGadgetSource,
    LbpPlanDesign,
    LbpPlanHierarchy,
    LbpPlanRealizationError,
    LbpRoutedConnection,
    LbpThingEndpoint,
    LbpThingKind,
)
from gateforge.providers.lbp.realize import (
    lbp_attachment_endpoint,
    lbp_constant_gadget_id,
    lbp_object_gadget_id,
)
from gateforge.target import PortDirection


_BOARD_GRID = 52.5
_BOARD_MARGIN = 105.0
_BOARD_MIN_X = 420.0
_BOARD_MIN_Y = 262.5


def containerize_lbp_plan(
    plan: LbpPlanDesign,
    material: MaterialDesign,
    graph: MaterialGraph,
    placed: PlacedDesign,
    policy: PhysicalHierarchyPolicy,
    providers: Mapping[str, TargetProvider],
) -> LbpPlanDesign:
    retained = retained_module_paths(material, policy, providers)
    if policy.mode == PhysicalHierarchyMode.FLAT or len(retained) <= 1:
        return plan
    modules = {item.path: item for item in material.modules}
    roots = [item for item in material.modules if item.parent is None]
    if len(roots) != 1:
        raise LbpPlanRealizationError(
            f"LBP hierarchy requires one root module, found {len(roots)}"
        )
    root = roots[0]
    parent_by_path = {
        path: nearest_retained_parent(modules[path], modules, retained)
        for path in retained
    }
    children_by_path: dict[str, list[str]] = defaultdict(list)
    for path, parent in parent_by_path.items():
        if parent is not None:
            children_by_path[parent].append(path)
    for children in children_by_path.values():
        children.sort()

    object_owner = {
        material_object.identifier: _retained_owner(
            material_object.hierarchy,
            modules,
            retained,
        )
        for material_object in material.objects
    }
    gadget_owner: dict[LbpGadgetId, str] = {}
    for material_object in material.objects:
        gadget_owner[lbp_object_gadget_id(material_object.identifier.value)] = (
            object_owner[material_object.identifier]
        )
    for gadget in plan.gadgets:
        if gadget.source == LbpGadgetSource.MODULE_PORT:
            gadget_owner[gadget.identifier] = root.path

    for gadget in plan.gadgets:
        if gadget.source != LbpGadgetSource.CONSTANT:
            continue
        target_owners: list[str] = []
        for dependency in graph.dependencies:
            if not isinstance(dependency.source, MaterialConstantRef):
                continue
            if lbp_constant_gadget_id(
                dependency.net,
                dependency.source.value,
            ) != gadget.identifier:
                continue
            target_endpoint = lbp_attachment_endpoint(
                dependency.target,
                dependency.net,
                source=False,
            )
            target_owner = gadget_owner.get(target_endpoint.gadget)
            if target_owner is not None:
                target_owners.append(target_owner)
        gadget_owner[gadget.identifier] = _lowest_common_container(
            target_owners,
            parent_by_path,
            root.path,
        )

    global_placements = {item.gadget: item for item in plan.placements}
    objects_by_module: dict[str, set[MaterialObjectId]] = {
        item.path: set(item.objects) for item in material.modules
    }

    def descendant_objects(path: str) -> set[MaterialObjectId]:
        result = set(objects_by_module[path])
        for child in modules[path].children:
            result.update(descendant_objects(child))
        return result

    centers: dict[str, tuple[float, float]] = {root.path: (0.0, 0.0)}
    descendants = {path: descendant_objects(path) for path in retained}
    for path in sorted(retained, key=lambda item: item.count("/")):
        if path == root.path:
            continue
        points = [
            global_placements[lbp_object_gadget_id(identifier.value)]
            for identifier in descendants[path]
            if lbp_object_gadget_id(identifier.value) in global_placements
        ]
        if points:
            centers[path] = (
                (min(item.x for item in points) + max(item.x for item in points)) / 2,
                (min(item.y for item in points) + max(item.y for item in points)) / 2,
            )
        else:
            parent = parent_by_path[path]
            centers[path] = centers[parent] if parent is not None else (0.0, 0.0)

    input_ports: dict[str, dict[MaterialNetId, int]] = {}
    output_ports: dict[str, dict[MaterialNetId, int]] = {}
    for path in retained:
        if path == root.path:
            input_ports[path] = {}
            output_ports[path] = {}
            continue
        occurrence = modules[path]
        input_ports[path] = _boundary_ports(occurrence, PortDirection.INPUT)
        output_ports[path] = _boundary_ports(occurrence, PortDirection.OUTPUT)

    routed: set[LbpRoutedConnection] = set()
    for dependency in graph.dependencies:
        source = lbp_attachment_endpoint(
            dependency.source,
            dependency.net,
            source=True,
        )
        target = lbp_attachment_endpoint(
            dependency.target,
            dependency.net,
            source=False,
        )
        source_owner = gadget_owner[source.gadget]
        target_owner = gadget_owner[target.gadget]
        lca = _lowest_common_container(
            [source_owner, target_owner],
            parent_by_path,
            root.path,
        )
        current = LbpThingEndpoint(
            LbpThingKind.GADGET,
            source.gadget.value,
            source.port,
        )
        current_owner = source_owner
        while current_owner != lca:
            try:
                port = output_ports[current_owner][dependency.net]
            except KeyError as error:
                raise LbpPlanRealizationError(
                    f"Module {current_owner!r} has no output boundary for material "
                    f"net {dependency.net.value}"
                ) from error
            routed.add(
                LbpRoutedConnection(
                    current,
                    LbpThingEndpoint(LbpThingKind.BOARD, current_owner, port),
                )
            )
            current = LbpThingEndpoint(
                LbpThingKind.MICROCHIP,
                current_owner,
                port,
            )
            parent = parent_by_path[current_owner]
            if parent is None:
                raise LbpPlanRealizationError("Cannot route above hierarchy root")
            current_owner = parent

        downward: list[str] = []
        cursor = target_owner
        while cursor != lca:
            downward.append(cursor)
            parent = parent_by_path[cursor]
            if parent is None:
                raise LbpPlanRealizationError("Cannot route below hierarchy root")
            cursor = parent
        for child in reversed(downward):
            try:
                port = input_ports[child][dependency.net]
            except KeyError as error:
                raise LbpPlanRealizationError(
                    f"Module {child!r} has no input boundary for material net "
                    f"{dependency.net.value}"
                ) from error
            routed.add(
                LbpRoutedConnection(
                    current,
                    LbpThingEndpoint(LbpThingKind.MICROCHIP, child, port + 1),
                )
            )
            current = LbpThingEndpoint(LbpThingKind.BOARD, child, port)
        routed.add(
            LbpRoutedConnection(
                current,
                LbpThingEndpoint(
                    LbpThingKind.GADGET,
                    target.gadget.value,
                    target.port,
                ),
            )
        )

    direct_gadgets: dict[str, list[LbpGadgetId]] = defaultdict(list)
    for gadget in plan.gadgets:
        direct_gadgets[gadget_owner[gadget.identifier]].append(gadget.identifier)
    note_owner = root.path
    direct_notes = {path: [] for path in retained}
    direct_notes[note_owner] = [item.identifier for item in plan.notes]

    containers: list[LbpContainer] = []
    for path in sorted(retained):
        center_x, center_y = centers[path]
        parent = parent_by_path[path]
        parent_center = centers[parent] if parent is not None else (0.0, 0.0)
        component_points: list[tuple[float, float]] = []
        for gadget_id in direct_gadgets[path]:
            placement = global_placements[gadget_id]
            component_points.append((placement.x - center_x, placement.y - center_y))
        for note in plan.notes:
            if note.identifier in direct_notes[path]:
                component_points.append((note.x - center_x, note.y - center_y))
        for child in children_by_path[path]:
            child_center = centers[child]
            component_points.append(
                (child_center[0] - center_x, child_center[1] - center_y)
            )
        board_size = _board_size(component_points)
        occurrence = modules[path]
        containers.append(
            LbpContainer(
                path=path,
                parent=parent,
                name=occurrence.instance or occurrence.module,
                input_nets=tuple(
                    net.value
                    for net, _ in sorted(
                        input_ports[path].items(), key=lambda item: item[1]
                    )
                ),
                output_nets=tuple(
                    net.value
                    for net, _ in sorted(
                        output_ports[path].items(), key=lambda item: item[1]
                    )
                ),
                gadgets=tuple(sorted(direct_gadgets[path], key=lambda item: item.value)),
                notes=tuple(
                    sorted(direct_notes[path], key=lambda item: item.value)
                ),
                children=tuple(children_by_path[path]),
                x=center_x - parent_center[0],
                y=center_y - parent_center[1],
                board_size=board_size,
            )
        )

    hierarchy = LbpPlanHierarchy(root.path, tuple(containers), tuple(routed))
    root_container = next(item for item in containers if item.path == root.path)
    return replace(plan, board_size=root_container.board_size, hierarchy=hierarchy)


def _retained_owner(
    path: str,
    modules: Mapping[str, MaterialModuleOccurrence],
    retained: frozenset[str],
) -> str:
    cursor: str | None = path
    while cursor is not None:
        if cursor in retained:
            return cursor
        cursor = modules[cursor].parent
    raise LbpPlanRealizationError(f"No retained owner for module path {path!r}")


def _boundary_ports(
    occurrence: MaterialModuleOccurrence,
    direction: PortDirection,
) -> dict[MaterialNetId, int]:
    result: dict[MaterialNetId, int] = {}
    for port in occurrence.ports:
        if port.direction != direction or port.net is None:
            continue
        if port.net in result:
            raise LbpPlanRealizationError(
                f"Module {occurrence.path!r} exposes material net {port.net.value} "
                f"on multiple {direction.value} ports"
            )
        result[port.net] = len(result)
    return result


def _lowest_common_container(
    paths: list[str],
    parents: Mapping[str, str | None],
    root: str,
) -> str:
    if not paths:
        return root
    ancestor_sets = []
    for path in paths:
        ancestors: list[str] = []
        cursor: str | None = path
        while cursor is not None:
            ancestors.append(cursor)
            cursor = parents[cursor]
        ancestor_sets.append(ancestors)
    for candidate in ancestor_sets[0]:
        if all(candidate in ancestors for ancestors in ancestor_sets[1:]):
            return candidate
    return root


def _board_size(points: list[tuple[float, float]]) -> LbpBoardSize:
    max_x = max((abs(x) for x, _ in points), default=0.0)
    max_y = max((abs(y) for _, y in points), default=0.0)
    return LbpBoardSize(
        max(_BOARD_MIN_X, _snap_up(max_x + _BOARD_MARGIN)),
        max(_BOARD_MIN_Y, _snap_up(max_y + _BOARD_MARGIN)),
    )


def _snap_up(value: float) -> float:
    return math.ceil(value / _BOARD_GRID - 1e-12) * _BOARD_GRID
