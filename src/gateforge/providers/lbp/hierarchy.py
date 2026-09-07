from __future__ import annotations

from collections import defaultdict
from collections.abc import Mapping
from dataclasses import replace
import math
import re

from gateforge.graph import MaterialGraph
from gateforge.hierarchy import (
    GeneratedHierarchyPolicy,
    PhysicalHierarchyMode,
    PhysicalHierarchyPolicy,
    nearest_retained_parent,
    retained_implementation_paths,
    retained_module_paths,
)
from gateforge.material import (
    MaterialConstantRef,
    MaterialDesign,
    MaterialImplementationOccurrence,
    MaterialModuleOccurrence,
    MaterialNetId,
    MaterialObjectId,
)
from gateforge.provider import TargetProvider
from gateforge.providers.lbp.plan import (
    LbpBoardSize,
    LbpContainer,
    LbpGadgetId,
    LbpGadgetPlacement,
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
_REGISTER_BIT_ROLE = re.compile(
    r"(?:sync_data|enable_data|enable_hold|enable_mux|invert_d|write_true|"
    r"write_false|force_reset|storage)_([0-9]+)$"
)


def containerize_lbp_plan(
    plan: LbpPlanDesign,
    material: MaterialDesign,
    graph: MaterialGraph,
    policy: PhysicalHierarchyPolicy,
    providers: Mapping[str, TargetProvider],
    generated_policy: GeneratedHierarchyPolicy = GeneratedHierarchyPolicy(),
) -> LbpPlanDesign:
    retained_modules = retained_module_paths(material, policy, providers)
    retained_implementations = retained_implementation_paths(
        material,
        generated_policy,
    )
    if not retained_implementations and (
        policy.mode == PhysicalHierarchyMode.FLAT or len(retained_modules) <= 1
    ):
        return plan
    modules = {item.path: item for item in material.modules}
    implementations = {
        item.path: item
        for item in material.implementations
        if item.path in retained_implementations
    }
    roots = [item for item in material.modules if item.parent is None]
    if len(roots) != 1:
        raise LbpPlanRealizationError(
            f"LBP hierarchy requires one root module, found {len(roots)}"
        )
    root = roots[0]
    parent_by_path = {
        path: nearest_retained_parent(modules[path], modules, retained_modules)
        for path in retained_modules
    }
    parent_by_path.update(
        {
            path: _retained_owner(
                implementation.owner_module,
                modules,
                retained_modules,
            )
            for path, implementation in implementations.items()
        }
    )
    retained = frozenset(parent_by_path)
    children_by_path: dict[str, list[str]] = defaultdict(list)
    for path, parent in parent_by_path.items():
        if parent is not None:
            children_by_path[parent].append(path)
    for children in children_by_path.values():
        children.sort()

    implementation_owner = {
        identifier: path
        for path, implementation in implementations.items()
        for identifier in implementation.objects
    }
    object_owner = {
        material_object.identifier: implementation_owner.get(
            material_object.identifier,
            _retained_owner(
                material_object.hierarchy,
                modules,
                retained_modules,
            ),
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
    global_notes = {item.identifier: item for item in plan.notes}
    _compact_register_implementations(
        implementations,
        material,
        global_placements,
    )
    objects_by_container: dict[str, set[MaterialObjectId]] = {
        path: set() for path in retained
    }
    for identifier, owner in object_owner.items():
        objects_by_container[owner].add(identifier)

    def descendant_objects(path: str) -> set[MaterialObjectId]:
        result = set(objects_by_container[path])
        for child in children_by_path[path]:
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
        if path in implementations:
            occurrence = implementations[path]
            input_ports[path] = _implementation_boundary_ports(
                occurrence,
                PortDirection.INPUT,
            )
            output_ports[path] = _implementation_boundary_ports(
                occurrence,
                PortDirection.OUTPUT,
            )
        else:
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
    if retained_implementations:
        _compact_parent_containers(
            retained,
            implementations,
            children_by_path,
            direct_gadgets,
            direct_notes,
            routed,
            centers,
            global_placements,
            global_notes,
            plan,
        )

    containers: list[LbpContainer] = []
    for path in sorted(retained):
        center_x, center_y = centers[path]
        parent = parent_by_path[path]
        parent_center = centers[parent] if parent is not None else (0.0, 0.0)
        component_points: list[tuple[float, float]] = []
        for gadget_id in direct_gadgets[path]:
            placement = global_placements[gadget_id]
            component_points.append((placement.x - center_x, placement.y - center_y))
        for note_id in direct_notes[path]:
            note = global_notes[note_id]
            component_points.append((note.x - center_x, note.y - center_y))
        for child in children_by_path[path]:
            child_center = centers[child]
            component_points.append(
                (child_center[0] - center_x, child_center[1] - center_y)
            )
        board_size = _board_size(component_points)
        occurrence = implementations.get(path) or modules[path]
        name = (
            occurrence.name
            if isinstance(occurrence, MaterialImplementationOccurrence)
            else occurrence.instance or occurrence.module
        )
        containers.append(
            LbpContainer(
                path=path,
                parent=parent,
                name=name,
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
    return replace(
        plan,
        placements=tuple(global_placements.values()),
        notes=tuple(global_notes.values()),
        board_size=root_container.board_size,
        hierarchy=hierarchy,
    )


def _compact_register_implementations(
    implementations: Mapping[str, MaterialImplementationOccurrence],
    material: MaterialDesign,
    placements: dict[LbpGadgetId, LbpGadgetPlacement],
) -> None:
    objects = {item.identifier: item for item in material.objects}
    for implementation in implementations.values():
        if implementation.mapper not in {
            "lbp.register_bank.coarse",
            "lbp.register_bank.scalar",
        }:
            continue
        roles = {
            objects[identifier].role: lbp_object_gadget_id(identifier.value)
            for identifier in implementation.objects
        }
        if "edge" not in roles:
            continue
        gadget_ids = tuple(roles.values())
        existing = [placements[identifier] for identifier in gadget_ids]
        center_x = (min(item.x for item in existing) + max(item.x for item in existing)) / 2
        center_y = (min(item.y for item in existing) + max(item.y for item in existing)) / 2
        local = _compact_register_role_positions(roles)
        local_center_x = (min(x for x, _ in local.values()) + max(x for x, _ in local.values())) / 2
        local_center_y = (min(y for _, y in local.values()) + max(y for _, y in local.values())) / 2
        for role, identifier in roles.items():
            old = placements[identifier]
            x, y = local[role]
            placements[identifier] = replace(
                old,
                x=center_x + x - local_center_x,
                y=center_y + y - local_center_y,
            )


def _compact_register_role_positions(
    roles: Mapping[str, LbpGadgetId],
) -> dict[str, tuple[float, float]]:
    bits = sorted(
        {
            int(match.group(1))
            for role in roles
            if (match := _REGISTER_BIT_ROLE.fullmatch(role)) is not None
        }
    )
    if not bits or bits != list(range(len(bits))):
        raise LbpPlanRealizationError(
            "Compact register bank roles must contain contiguous bit indices"
        )
    asynchronous = any(role.startswith("force_reset_") for role in roles)
    synchronous = any(role.startswith("sync_data_") for role in roles)
    enabled_bits = {
        int(role.rsplit("_", 1)[1])
        for role in roles
        if role.startswith("enable_mux_")
    }
    column_count = max(1, math.ceil(math.sqrt(len(bits))))
    row_count = math.ceil(len(bits) / column_count)
    if enabled_bits:
        tile_pitch_x = 945.0 if asynchronous else 787.5
    else:
        tile_pitch_x = 630.0 if asynchronous else (577.5 if synchronous else 420.0)
    tile_pitch_y = 367.5 if asynchronous else 315.0
    branch_y = 105.0 if asynchronous else 78.75
    result: dict[str, tuple[float, float]] = {}
    for bit in bits:
        column = bit % column_count
        row = bit // column_count
        base_x = (column - (column_count - 1) / 2) * tile_pitch_x
        base_y = (row - (row_count - 1) / 2) * tile_pitch_y
        if synchronous:
            result[f"sync_data_{bit}"] = (
                base_x - (315.0 if bit in enabled_bits else 262.5),
                base_y,
            )
        if bit in enabled_bits:
            result[f"enable_data_{bit}"] = (base_x - 472.5, base_y - branch_y)
            result[f"enable_hold_{bit}"] = (base_x - 472.5, base_y + branch_y)
            result[f"enable_mux_{bit}"] = (base_x - 315.0, base_y)
        result[f"invert_d_{bit}"] = (
            base_x
            - (
                105.0
                if bit in enabled_bits
                else (210.0 if asynchronous else 157.5)
            ),
            base_y,
        )
        result[f"write_true_{bit}"] = (
            base_x + (52.5 if bit in enabled_bits else (-52.5 if asynchronous else 0.0)),
            base_y - branch_y,
        )
        result[f"write_false_{bit}"] = (
            base_x + (52.5 if bit in enabled_bits else (-52.5 if asynchronous else 0.0)),
            base_y + branch_y,
        )
        if asynchronous:
            result[f"force_reset_{bit}"] = (
                base_x + (262.5 if bit in enabled_bits else 157.5),
                base_y,
            )
            storage_x = base_x + (420.0 if bit in enabled_bits else 315.0)
        else:
            storage_x = base_x + (262.5 if bit in enabled_bits else 157.5)
        result[f"storage_{bit}"] = (storage_x, base_y)

    header_y = min(y for _, y in result.values()) - tile_pitch_y
    controls = [
        role
        for role in ("invert_clock", "edge", "invert_reset")
        if role in roles
    ]
    controls.extend(sorted(role for role in roles if role.startswith("invert_enable_")))
    for index, role in enumerate(controls):
        result[role] = (
            (index - (len(controls) - 1) / 2) * 210.0,
            header_y,
        )
    missing = set(roles) - set(result)
    if missing:
        raise LbpPlanRealizationError(
            f"Compact register bank has unsupported roles {sorted(missing)!r}"
        )
    return result


type _ContainerNode = tuple[str, str]


def _compact_parent_containers(
    retained: frozenset[str],
    implementations: Mapping[str, MaterialImplementationOccurrence],
    children_by_path: Mapping[str, list[str]],
    direct_gadgets: Mapping[str, list[LbpGadgetId]],
    direct_notes,
    routed: set[LbpRoutedConnection],
    centers: dict[str, tuple[float, float]],
    placements: dict[LbpGadgetId, LbpGadgetPlacement],
    notes,
    plan: LbpPlanDesign,
) -> None:
    gadgets = {item.identifier: item for item in plan.gadgets}
    module_paths = sorted(
        (path for path in retained if path not in implementations),
        key=lambda path: (path.count("/"), path),
    )
    for path in module_paths:
        nodes = {
            *(('gadget', identifier.value) for identifier in direct_gadgets[path]),
            *(('child', child) for child in children_by_path[path]),
        }
        if not nodes:
            continue
        old_positions: dict[_ContainerNode, tuple[float, float]] = {
            ("gadget", identifier.value): (
                placements[identifier].x,
                placements[identifier].y,
            )
            for identifier in direct_gadgets[path]
        }
        old_positions.update(
            {
                ("child", child): centers[child]
                for child in children_by_path[path]
            }
        )
        edges = {
            (source, target)
            for connection in routed
            if (source := _container_node(connection.source)) in nodes
            and (target := _container_node(connection.target)) in nodes
        }
        local = _layered_container_positions(nodes, edges, old_positions)
        center_x, center_y = centers[path]
        gadget_deltas: dict[LbpGadgetId, tuple[float, float]] = {}
        for identifier in direct_gadgets[path]:
            old = placements[identifier]
            local_x, local_y = local[("gadget", identifier.value)]
            new_x = center_x + local_x
            new_y = center_y + local_y
            gadget_deltas[identifier] = (new_x - old.x, new_y - old.y)
            placements[identifier] = replace(old, x=new_x, y=new_y)

        port_gadgets = [
            identifier
            for identifier in direct_gadgets[path]
            if gadgets[identifier].source == LbpGadgetSource.MODULE_PORT
        ]
        for note_id in direct_notes[path]:
            note = notes[note_id]
            if not port_gadgets:
                continue
            owner = min(
                port_gadgets,
                key=lambda identifier: (
                    (old_positions[("gadget", identifier.value)][0] - note.x) ** 2
                    + (old_positions[("gadget", identifier.value)][1] - note.y) ** 2,
                    identifier.value,
                ),
            )
            delta_x, delta_y = gadget_deltas[owner]
            notes[note_id] = replace(
                note,
                x=note.x + delta_x,
                y=note.y + delta_y,
            )

        for child in children_by_path[path]:
            local_x, local_y = local[("child", child)]
            new_center = (center_x + local_x, center_y + local_y)
            old_center = centers[child]
            _translate_container_subtree(
                child,
                new_center[0] - old_center[0],
                new_center[1] - old_center[1],
                children_by_path,
                direct_gadgets,
                direct_notes,
                centers,
                placements,
                notes,
            )


def _container_node(endpoint: LbpThingEndpoint) -> _ContainerNode:
    if endpoint.kind == LbpThingKind.GADGET:
        return ("gadget", endpoint.identifier)
    if endpoint.kind == LbpThingKind.MICROCHIP:
        return ("child", endpoint.identifier)
    return ("board", endpoint.identifier)


def _layered_container_positions(
    nodes: set[_ContainerNode],
    edges: set[tuple[_ContainerNode, _ContainerNode]],
    old_positions: Mapping[_ContainerNode, tuple[float, float]],
) -> dict[_ContainerNode, tuple[float, float]]:
    successors = {node: set() for node in nodes}
    predecessors = {node: set() for node in nodes}
    for source, target in edges:
        if source == target:
            continue
        successors[source].add(target)
        predecessors[target].add(source)

    components = _strong_components(nodes, successors, predecessors)
    component_of = {
        node: index
        for index, component in enumerate(components)
        for node in component
    }
    component_predecessors = {index: set() for index in range(len(components))}
    component_successors = {index: set() for index in range(len(components))}
    for source, targets in successors.items():
        source_component = component_of[source]
        for target in targets:
            target_component = component_of[target]
            if source_component == target_component:
                continue
            component_successors[source_component].add(target_component)
            component_predecessors[target_component].add(source_component)

    indegree = {
        index: len(component_predecessors[index])
        for index in range(len(components))
    }
    ready = sorted(index for index, degree in indegree.items() if degree == 0)
    depths: dict[int, int] = {}
    while ready:
        component = ready.pop(0)
        depths[component] = max(
            (depths[parent] + 1 for parent in component_predecessors[component]),
            default=0,
        )
        for successor in sorted(component_successors[component]):
            indegree[successor] -= 1
            if indegree[successor] == 0:
                ready.append(successor)
                ready.sort()
    if len(depths) != len(components):
        raise LbpPlanRealizationError("Container SCC condensation graph is cyclic")

    by_depth: dict[int, list[_ContainerNode]] = defaultdict(list)
    for node in nodes:
        by_depth[depths[component_of[node]]].append(node)
    row_limit = max(4, math.ceil(math.sqrt(len(nodes))))
    positions: dict[_ContainerNode, tuple[float, float]] = {}
    physical_column = 0
    for depth in sorted(by_depth):
        ordered = sorted(
            by_depth[depth],
            key=lambda node: (old_positions[node][1], node),
        )
        for start in range(0, len(ordered), row_limit):
            chunk = ordered[start : start + row_limit]
            for row, node in enumerate(chunk):
                positions[node] = (
                    physical_column * 210.0,
                    (row - (len(chunk) - 1) / 2) * 210.0,
                )
            physical_column += 1
    min_x = min(x for x, _ in positions.values())
    max_x = max(x for x, _ in positions.values())
    min_y = min(y for _, y in positions.values())
    max_y = max(y for _, y in positions.values())
    center_x = (min_x + max_x) / 2
    center_y = (min_y + max_y) / 2
    return {
        node: (x - center_x, y - center_y)
        for node, (x, y) in positions.items()
    }


def _strong_components(
    nodes: set[_ContainerNode],
    successors: Mapping[_ContainerNode, set[_ContainerNode]],
    predecessors: Mapping[_ContainerNode, set[_ContainerNode]],
) -> tuple[tuple[_ContainerNode, ...], ...]:
    visited: set[_ContainerNode] = set()
    finished: list[_ContainerNode] = []
    for root in sorted(nodes):
        if root in visited:
            continue
        stack: list[tuple[_ContainerNode, bool]] = [(root, False)]
        while stack:
            node, expanded = stack.pop()
            if expanded:
                finished.append(node)
                continue
            if node in visited:
                continue
            visited.add(node)
            stack.append((node, True))
            for successor in sorted(successors[node], reverse=True):
                if successor not in visited:
                    stack.append((successor, False))

    assigned: set[_ContainerNode] = set()
    components: list[tuple[_ContainerNode, ...]] = []
    for root in reversed(finished):
        if root in assigned:
            continue
        component: set[_ContainerNode] = set()
        component_stack: list[_ContainerNode] = [root]
        assigned.add(root)
        while component_stack:
            node = component_stack.pop()
            component.add(node)
            for predecessor in sorted(predecessors[node], reverse=True):
                if predecessor not in assigned:
                    assigned.add(predecessor)
                    component_stack.append(predecessor)
        components.append(tuple(sorted(component)))
    return tuple(sorted(components))


def _translate_container_subtree(
    path: str,
    delta_x: float,
    delta_y: float,
    children_by_path: Mapping[str, list[str]],
    direct_gadgets: Mapping[str, list[LbpGadgetId]],
    direct_notes,
    centers: dict[str, tuple[float, float]],
    placements: dict[LbpGadgetId, LbpGadgetPlacement],
    notes,
) -> None:
    stack = [path]
    while stack:
        current = stack.pop()
        center_x, center_y = centers[current]
        centers[current] = (center_x + delta_x, center_y + delta_y)
        for identifier in direct_gadgets[current]:
            old = placements[identifier]
            placements[identifier] = replace(
                old,
                x=old.x + delta_x,
                y=old.y + delta_y,
            )
        for note_id in direct_notes[current]:
            note = notes[note_id]
            notes[note_id] = replace(
                note,
                x=note.x + delta_x,
                y=note.y + delta_y,
            )
        stack.extend(children_by_path[current])


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


def _implementation_boundary_ports(
    occurrence: MaterialImplementationOccurrence,
    direction: PortDirection,
) -> dict[MaterialNetId, int]:
    result: dict[MaterialNetId, int] = {}
    for port in sorted(occurrence.ports, key=lambda item: (item.name, item.bit)):
        if port.direction != direction or port.net in result:
            continue
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
