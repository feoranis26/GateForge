from __future__ import annotations

from collections import defaultdict
from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType

from gateforge.graph import (
    ConstantSubject,
    MaterialGraph,
    MaterialSubject,
    ModulePortSubject,
    ObjectSubject,
    material_subject_key,
)
from gateforge.hierarchy import (
    GeneratedHierarchyPolicy,
    PhysicalHierarchyPolicy,
    nearest_retained_parent,
    retained_implementation_paths,
    retained_module_paths,
)
from gateforge.material import (
    MaterialAttachment,
    MaterialConstantRef,
    MaterialModulePortRef,
    MaterialNetId,
    MaterialObjectId,
    MaterialObjectPortRef,
)
from gateforge.placement.model import ContainerKind, PlacementError
from gateforge.provider import TargetProvider
from gateforge.target import PortDirection


@dataclass(frozen=True, slots=True, order=True)
class ChildContainerSubject:
    path: str


@dataclass(frozen=True, slots=True, order=True)
class BoundaryPortSubject:
    net: MaterialNetId
    direction: PortDirection


type LocalPlacementSubject = (
    MaterialSubject | ChildContainerSubject | BoundaryPortSubject
)


@dataclass(frozen=True, slots=True)
class LocalDependencyEndpoint:
    subject: LocalPlacementSubject
    attachment: MaterialAttachment | None = None


@dataclass(frozen=True, slots=True)
class LocalPlacementDependency:
    net: MaterialNetId
    source: LocalDependencyEndpoint
    target: LocalDependencyEndpoint


@dataclass(frozen=True, slots=True)
class PlacementContainerTopology:
    path: str
    kind: ContainerKind
    name: str
    parent: str | None
    children: tuple[str, ...]
    subjects: tuple[LocalPlacementSubject, ...]
    dependencies: tuple[LocalPlacementDependency, ...]
    predecessors: Mapping[LocalPlacementSubject, frozenset[LocalPlacementSubject]]
    successors: Mapping[LocalPlacementSubject, frozenset[LocalPlacementSubject]]
    incident_nets: Mapping[LocalPlacementSubject, frozenset[MaterialNetId]]
    boundary_ports: tuple[BoundaryPortSubject, ...]


@dataclass(frozen=True, slots=True)
class PlacementHierarchyTopology:
    root: str
    containers: tuple[PlacementContainerTopology, ...]
    parents: Mapping[str, str | None]
    subject_owners: Mapping[MaterialSubject, str]
    object_owners: Mapping[MaterialObjectId, str]

    def container(self, path: str) -> PlacementContainerTopology:
        try:
            return next(item for item in self.containers if item.path == path)
        except StopIteration as error:
            raise PlacementError(f"Unknown placement container {path!r}") from error


def build_placement_hierarchy(
    graph: MaterialGraph,
    providers: Mapping[str, TargetProvider],
    physical_hierarchy: PhysicalHierarchyPolicy,
    generated_hierarchy: GeneratedHierarchyPolicy,
) -> PlacementHierarchyTopology:
    design = graph.design
    modules = {item.path: item for item in design.modules}
    implementations = {item.path: item for item in design.implementations}
    retained_modules = retained_module_paths(design, physical_hierarchy, providers)
    retained_implementations = retained_implementation_paths(
        design,
        generated_hierarchy,
    )

    if modules:
        roots = [item for item in design.modules if item.parent is None]
        if len(roots) != 1:
            raise PlacementError(
                f"Placement hierarchy requires one root module, found {len(roots)}"
            )
        root = roots[0].path
    else:
        module_names = sorted(
            {
                subject.module
                for subject in graph.subjects
                if isinstance(subject, ModulePortSubject)
            }
        )
        root_name = module_names[0] if len(module_names) == 1 else "material"
        root = f"module:{root_name}"
        retained_modules = frozenset({root})

    parent_by_path: dict[str, str | None] = {}
    names: dict[str, str] = {}
    kinds: dict[str, ContainerKind] = {}
    for path in retained_modules:
        if path in modules:
            occurrence = modules[path]
            parent_by_path[path] = nearest_retained_parent(
                occurrence,
                modules,
                retained_modules,
            )
            names[path] = occurrence.instance or occurrence.module
        else:
            parent_by_path[path] = None
            names[path] = path.removeprefix("module:")
        kinds[path] = ContainerKind.MODULE
    for path in retained_implementations:
        implementation = implementations[path]
        parent_by_path[path] = _retained_owner(
            implementation.owner_module,
            modules,
            retained_modules,
            root,
        )
        names[path] = implementation.name
        kinds[path] = ContainerKind.IMPLEMENTATION

    if parent_by_path.get(root) is not None:
        raise PlacementError("Placement hierarchy root must not have a parent")
    children_by_path: dict[str, list[str]] = defaultdict(list)
    for path, parent in parent_by_path.items():
        if parent is not None:
            children_by_path[parent].append(path)
    for children in children_by_path.values():
        children.sort()

    implementation_owner = {
        identifier: path
        for path in retained_implementations
        for identifier in implementations[path].objects
    }
    object_owners = {
        item.identifier: implementation_owner.get(
            item.identifier,
            _retained_owner(
                item.hierarchy,
                modules,
                retained_modules,
                root,
            ),
        )
        for item in design.objects
    }

    subject_owners: dict[MaterialSubject, str] = {}
    for subject in graph.subjects:
        if isinstance(subject, ObjectSubject):
            subject_owners[subject] = object_owners[subject.object]
        elif isinstance(subject, ModulePortSubject):
            subject_owners[subject] = root

    for subject in graph.subjects:
        if not isinstance(subject, ConstantSubject):
            continue
        consumers = [
            subject_owners[item]
            for item in graph.successors[subject]
            if item in subject_owners
        ]
        subject_owners[subject] = _lowest_common_container(
            consumers,
            parent_by_path,
            root,
        )

    subjects_by_path: dict[str, set[LocalPlacementSubject]] = {
        path: set() for path in parent_by_path
    }
    dependencies_by_path: dict[
        str,
        set[LocalPlacementDependency],
    ] = {path: set() for path in parent_by_path}
    nets_by_path: dict[
        str,
        dict[LocalPlacementSubject, set[MaterialNetId]],
    ] = {
        path: defaultdict(set)
        for path in parent_by_path
    }
    for subject, owner in subject_owners.items():
        subjects_by_path[owner].add(subject)
        nets_by_path[owner][subject].update(graph.incident_nets[subject])
    for parent, children in children_by_path.items():
        for child in children:
            subjects_by_path[parent].add(ChildContainerSubject(child))

    for dependency in graph.dependencies:
        source = _attachment_subject(dependency.net, dependency.source)
        target = _attachment_subject(dependency.net, dependency.target)
        _route_dependency(
            source,
            subject_owners[source],
            target,
            subject_owners[target],
            dependency.net,
            dependency.source,
            dependency.target,
            parent_by_path,
            root,
            subjects_by_path,
            dependencies_by_path,
            nets_by_path,
        )

    containers = []
    for path in sorted(parent_by_path):
        boundary_ports = tuple(
            sorted(
                (
                    item
                    for item in subjects_by_path[path]
                    if isinstance(item, BoundaryPortSubject)
                ),
                key=lambda item: _boundary_order_key(
                    path,
                    item,
                    modules,
                    implementations,
                ),
            )
        )
        subjects = tuple(sorted(subjects_by_path[path], key=local_subject_key))
        predecessors: dict[LocalPlacementSubject, set[LocalPlacementSubject]] = {
            item: set() for item in subjects
        }
        successors: dict[LocalPlacementSubject, set[LocalPlacementSubject]] = {
            item: set() for item in subjects
        }
        for dependency in dependencies_by_path[path]:
            successors[dependency.source.subject].add(dependency.target.subject)
            predecessors[dependency.target.subject].add(dependency.source.subject)
        containers.append(
            PlacementContainerTopology(
                path=path,
                kind=kinds[path],
                name=names[path],
                parent=parent_by_path[path],
                children=tuple(children_by_path[path]),
                subjects=subjects,
                dependencies=tuple(
                    sorted(
                        dependencies_by_path[path],
                        key=lambda item: (
                            item.net.value,
                            local_subject_key(item.source.subject),
                            local_subject_key(item.target.subject),
                            repr(item.source.attachment),
                            repr(item.target.attachment),
                        ),
                    )
                ),
                predecessors=MappingProxyType(
                    {item: frozenset(predecessors[item]) for item in subjects}
                ),
                successors=MappingProxyType(
                    {item: frozenset(successors[item]) for item in subjects}
                ),
                incident_nets=MappingProxyType(
                    {
                        item: frozenset(nets_by_path[path][item])
                        for item in subjects
                    }
                ),
                boundary_ports=boundary_ports,
            )
        )
    return PlacementHierarchyTopology(
        root,
        tuple(containers),
        MappingProxyType(parent_by_path),
        MappingProxyType(subject_owners),
        MappingProxyType(object_owners),
    )


def local_subject_key(subject: LocalPlacementSubject) -> tuple[object, ...]:
    if isinstance(subject, ChildContainerSubject):
        return (3, subject.path)
    if isinstance(subject, BoundaryPortSubject):
        side = 0 if subject.direction == PortDirection.INPUT else 2
        return (side, subject.net.value, subject.direction.value)
    return material_subject_key(subject)


def _route_dependency(
    source: MaterialSubject,
    source_owner: str,
    target: MaterialSubject,
    target_owner: str,
    net: MaterialNetId,
    source_attachment: MaterialAttachment,
    target_attachment: MaterialAttachment,
    parents: Mapping[str, str | None],
    root: str,
    subjects: dict[str, set[LocalPlacementSubject]],
    dependencies: dict[str, set[LocalPlacementDependency]],
    incident: dict[str, dict[LocalPlacementSubject, set[MaterialNetId]]],
) -> None:
    lca = _lowest_common_container([source_owner, target_owner], parents, root)
    current_source = LocalDependencyEndpoint(source, source_attachment)
    current_owner = source_owner
    while current_owner != lca:
        boundary = LocalDependencyEndpoint(
            BoundaryPortSubject(net, PortDirection.OUTPUT)
        )
        _add_dependency(
            current_owner,
            current_source,
            boundary,
            net,
            subjects,
            dependencies,
            incident,
        )
        child = current_owner
        parent = parents[child]
        if parent is None:
            raise PlacementError("Cannot route placement dependency above root")
        current_source = LocalDependencyEndpoint(ChildContainerSubject(child))
        current_owner = parent

    current_target = LocalDependencyEndpoint(target, target_attachment)
    current_owner = target_owner
    while current_owner != lca:
        boundary = LocalDependencyEndpoint(
            BoundaryPortSubject(net, PortDirection.INPUT)
        )
        _add_dependency(
            current_owner,
            boundary,
            current_target,
            net,
            subjects,
            dependencies,
            incident,
        )
        child = current_owner
        parent = parents[child]
        if parent is None:
            raise PlacementError("Cannot route placement dependency below root")
        current_target = LocalDependencyEndpoint(ChildContainerSubject(child))
        current_owner = parent

    _add_dependency(
        lca,
        current_source,
        current_target,
        net,
        subjects,
        dependencies,
        incident,
    )


def _add_dependency(
    path: str,
    source: LocalDependencyEndpoint,
    target: LocalDependencyEndpoint,
    net: MaterialNetId,
    subjects: dict[str, set[LocalPlacementSubject]],
    dependencies: dict[str, set[LocalPlacementDependency]],
    incident: dict[str, dict[LocalPlacementSubject, set[MaterialNetId]]],
) -> None:
    subjects[path].update((source.subject, target.subject))
    dependencies[path].add(LocalPlacementDependency(net, source, target))
    incident[path][source.subject].add(net)
    incident[path][target.subject].add(net)


def _retained_owner(
    path: str,
    modules: Mapping[str, object],
    retained: frozenset[str],
    root: str,
) -> str:
    if not modules:
        return root
    cursor: str | None = path
    while cursor is not None:
        if cursor in retained:
            return cursor
        occurrence = modules.get(cursor)
        if occurrence is None:
            raise PlacementError(f"Unknown material hierarchy path {path!r}")
        cursor = occurrence.parent
    raise PlacementError(f"No retained placement owner for {path!r}")


def _lowest_common_container(
    paths: list[str],
    parents: Mapping[str, str | None],
    root: str,
) -> str:
    if not paths:
        return root
    ancestors = []
    for path in paths:
        chain = []
        cursor: str | None = path
        while cursor is not None:
            chain.append(cursor)
            cursor = parents[cursor]
        ancestors.append(chain)
    for candidate in ancestors[0]:
        if all(candidate in chain for chain in ancestors[1:]):
            return candidate
    return root


def _boundary_order_key(
    path: str,
    boundary: BoundaryPortSubject,
    modules,
    implementations,
) -> tuple[object, ...]:
    occurrence = implementations.get(path) or modules.get(path)
    if occurrence is not None:
        matching = [
            index
            for index, port in enumerate(occurrence.ports)
            if port.net == boundary.net and port.direction == boundary.direction
        ]
        if matching:
            return (0, matching[0], boundary.net.value)
    return (1, boundary.direction.value, boundary.net.value)


def _attachment_subject(
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
    raise PlacementError(f"Unsupported material attachment {attachment!r}")