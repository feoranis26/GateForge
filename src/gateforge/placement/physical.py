from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
import math

from gateforge.graph import MaterialSubject
from gateforge.material import MaterialDesignDigest, MaterialNetId
from gateforge.placement.hierarchy import (
    BoundaryPortSubject,
    ChildContainerSubject,
    PlacementHierarchyTopology,
)
from gateforge.placement.model import (
    ContainerKind,
    PhysicalBounds,
    PhysicalConnection,
    PhysicalEndpoint,
    PhysicalEndpointKind,
    PlacementError,
)
from gateforge.target import PortDirection, ProviderConfiguration


@dataclass(frozen=True, slots=True)
class PhysicalLayoutRules:
    board_grid: float = 0.0
    board_margin_x: float = 0.0
    board_margin_y: float = 0.0
    minimum_board_half_width: float = 0.0
    minimum_board_half_height: float = 0.0
    facade_width: float = 1.0
    facade_port_pitch: float = 1.0
    facade_min_ports: int = 1

    def __post_init__(self) -> None:
        for attribute in (
            "board_grid",
            "board_margin_x",
            "board_margin_y",
            "minimum_board_half_width",
            "minimum_board_half_height",
        ):
            value = getattr(self, attribute)
            if (
                isinstance(value, bool)
                or not isinstance(value, (int, float))
                or not math.isfinite(float(value))
                or value < 0
            ):
                raise PlacementError(
                    f"Physical layout rule {attribute} must be finite and nonnegative"
                )
            object.__setattr__(self, attribute, float(value))
        for attribute in ("facade_width", "facade_port_pitch"):
            value = getattr(self, attribute)
            if (
                isinstance(value, bool)
                or not isinstance(value, (int, float))
                or not math.isfinite(float(value))
                or value <= 0
            ):
                raise PlacementError(
                    f"Physical layout rule {attribute} must be finite and positive"
                )
            object.__setattr__(self, attribute, float(value))
        if (
            not isinstance(self.facade_min_ports, int)
            or isinstance(self.facade_min_ports, bool)
            or self.facade_min_ports <= 0
        ):
            raise PlacementError("Physical facade_min_ports must be positive")


@dataclass(frozen=True, slots=True)
class PhysicalComponent:
    identifier: str
    provider: str
    kind: str
    source: MaterialSubject
    bounds: PhysicalBounds
    payload: ProviderConfiguration = ProviderConfiguration()

    def __post_init__(self) -> None:
        if not self.identifier or not self.provider or not self.kind:
            raise PlacementError(
                "Physical component ID, provider, and kind must not be empty"
            )


@dataclass(frozen=True, slots=True)
class PhysicalAnnotation:
    identifier: str
    provider: str
    text: str
    bounds: PhysicalBounds
    scale_x: float = 1.0
    scale_y: float = 1.0
    payload: ProviderConfiguration = ProviderConfiguration()

    def __post_init__(self) -> None:
        if not self.identifier or not self.provider or not self.text:
            raise PlacementError(
                "Physical annotation ID, provider, and text must not be empty"
            )
        for attribute in ("scale_x", "scale_y"):
            value = getattr(self, attribute)
            if (
                isinstance(value, bool)
                or not isinstance(value, (int, float))
                or not math.isfinite(float(value))
                or value <= 0
            ):
                raise PlacementError(
                    f"Physical annotation {attribute} must be finite and positive"
                )
            object.__setattr__(self, attribute, float(value))


@dataclass(frozen=True, slots=True)
class PhysicalMember:
    identifier: str
    x: float = 0.0
    y: float = 0.0
    angle: float = 0.0

    def __post_init__(self) -> None:
        if not self.identifier:
            raise PlacementError("Physical member ID must not be empty")
        for attribute in ("x", "y", "angle"):
            value = getattr(self, attribute)
            if (
                isinstance(value, bool)
                or not isinstance(value, (int, float))
                or not math.isfinite(float(value))
            ):
                raise PlacementError(
                    f"Physical member {attribute} must be finite"
                )
            object.__setattr__(self, attribute, float(value))


@dataclass(frozen=True, slots=True)
class PhysicalPrefabInstance:
    identifier: str
    subject: MaterialSubject
    bounds: PhysicalBounds
    components: tuple[PhysicalMember, ...]
    annotations: tuple[PhysicalMember, ...] = ()

    def __post_init__(self) -> None:
        if not self.identifier:
            raise PlacementError("Physical prefab instance ID must not be empty")
        components = tuple(sorted(self.components, key=lambda item: item.identifier))
        annotations = tuple(
            sorted(self.annotations, key=lambda item: item.identifier)
        )
        _require_unique(
            (item.identifier for item in components),
            "physical prefab component",
        )
        _require_unique(
            (item.identifier for item in annotations),
            "physical prefab annotation",
        )
        if not components:
            raise PlacementError("Physical prefab instance requires a component")
        object.__setattr__(self, "components", components)
        object.__setattr__(self, "annotations", annotations)


@dataclass(frozen=True, slots=True, order=True)
class PhysicalBoundaryPort:
    net: MaterialNetId
    direction: PortDirection
    index: int

    def __post_init__(self) -> None:
        if (
            not isinstance(self.index, int)
            or isinstance(self.index, bool)
            or self.index < 0
        ):
            raise PlacementError("Physical boundary port index must be nonnegative")


@dataclass(frozen=True, slots=True)
class PhysicalContainer:
    path: str
    kind: ContainerKind
    name: str
    parent: str | None
    children: tuple[str, ...]
    components: tuple[PhysicalComponent, ...]
    annotations: tuple[PhysicalAnnotation, ...]
    prefabs: tuple[PhysicalPrefabInstance, ...]
    boundary_ports: tuple[PhysicalBoundaryPort, ...]
    connections: tuple[PhysicalConnection, ...]

    def __post_init__(self) -> None:
        if not self.path or not self.name:
            raise PlacementError("Physical container path and name must not be empty")
        children = tuple(sorted(self.children))
        components = tuple(
            sorted(self.components, key=lambda item: item.identifier)
        )
        annotations = tuple(
            sorted(self.annotations, key=lambda item: item.identifier)
        )
        prefabs = tuple(sorted(self.prefabs, key=lambda item: item.identifier))
        boundary_ports = tuple(
            sorted(
                self.boundary_ports,
                key=lambda item: (item.direction.value, item.index, item.net.value),
            )
        )
        connections = tuple(sorted(set(self.connections)))
        _require_unique(children, "physical child container")
        _require_unique(
            (item.identifier for item in components),
            "physical component",
        )
        _require_unique(
            (item.identifier for item in annotations),
            "physical annotation",
        )
        _require_unique(
            (item.identifier for item in prefabs),
            "physical prefab instance",
        )
        component_ids = {item.identifier for item in components}
        annotation_ids = {item.identifier for item in annotations}
        component_references = tuple(
            member.identifier for prefab in prefabs for member in prefab.components
        )
        annotation_references = tuple(
            member.identifier for prefab in prefabs for member in prefab.annotations
        )
        if len(set(component_references)) != len(component_references):
            raise PlacementError(
                f"Physical container {self.path!r} has a component in multiple prefabs"
            )
        if len(set(annotation_references)) != len(annotation_references):
            raise PlacementError(
                f"Physical container {self.path!r} has an annotation in multiple prefabs"
            )
        used_components = set(component_references)
        used_annotations = set(annotation_references)
        if used_components != component_ids:
            raise PlacementError(
                f"Physical container {self.path!r} prefab/component coverage differs"
            )
        if used_annotations != annotation_ids:
            raise PlacementError(
                f"Physical container {self.path!r} prefab/annotation coverage differs"
            )
        component_by_id = {item.identifier: item for item in components}
        for prefab in prefabs:
            for member in prefab.components:
                if component_by_id[member.identifier].source != prefab.subject:
                    raise PlacementError(
                        f"Physical component {member.identifier!r} source differs "
                        "from its prefab"
                    )
        object.__setattr__(self, "children", children)
        object.__setattr__(self, "components", components)
        object.__setattr__(self, "annotations", annotations)
        object.__setattr__(self, "prefabs", prefabs)
        object.__setattr__(self, "boundary_ports", boundary_ports)
        object.__setattr__(self, "connections", connections)


@dataclass(frozen=True, slots=True)
class PhysicalDesign:
    material_digest: MaterialDesignDigest
    target: str
    root: str
    containers: tuple[PhysicalContainer, ...]
    topology: PlacementHierarchyTopology
    layout_rules: PhysicalLayoutRules = PhysicalLayoutRules()

    def __post_init__(self) -> None:
        if not self.target or not self.root:
            raise PlacementError("Physical design target and root must not be empty")
        containers = tuple(sorted(self.containers, key=lambda item: item.path))
        _require_unique((item.path for item in containers), "physical container")
        by_path = {item.path: item for item in containers}
        if self.root not in by_path:
            raise PlacementError("Physical design root container is missing")
        if by_path[self.root].parent is not None:
            raise PlacementError("Physical design root must not have a parent")
        topology_by_path = {
            item.path: item for item in self.topology.containers
        }
        for container in containers:
            for child in container.children:
                if child not in by_path or by_path[child].parent != container.path:
                    raise PlacementError(
                        f"Physical container {container.path!r} has invalid child "
                        f"{child!r}"
                    )
        if {item.path for item in self.topology.containers} != set(by_path):
            raise PlacementError("Physical design and topology containers differ")
        _require_unique(
            (
                component.identifier
                for container in containers
                for component in container.components
            ),
            "global physical component",
        )
        _require_unique(
            (
                annotation.identifier
                for container in containers
                for annotation in container.annotations
            ),
            "global physical annotation",
        )
        _require_unique(
            (
                prefab.identifier
                for container in containers
                for prefab in container.prefabs
            ),
            "global physical prefab instance",
        )
        for container in containers:
            topology = topology_by_path[container.path]
            if (
                container.kind != topology.kind
                or container.name != topology.name
                or container.parent != topology.parent
                or container.children != topology.children
            ):
                raise PlacementError(
                    f"Physical container {container.path!r} differs from its topology"
                )
            expected_subjects = tuple(
                subject
                for subject in topology.subjects
                if not isinstance(
                    subject,
                    (BoundaryPortSubject, ChildContainerSubject),
                )
            )
            actual_subjects = tuple(prefab.subject for prefab in container.prefabs)
            if len(set(actual_subjects)) != len(actual_subjects) or set(
                actual_subjects
            ) != set(expected_subjects):
                raise PlacementError(
                    f"Physical container {container.path!r} subject coverage differs"
                )
            expected_boundaries = {
                (item.net, item.direction) for item in topology.boundary_ports
            }
            actual_boundaries = {
                (item.net, item.direction) for item in container.boundary_ports
            }
            if actual_boundaries != expected_boundaries:
                raise PlacementError(
                    f"Physical container {container.path!r} boundary coverage differs"
                )
            for direction in PortDirection:
                indices = sorted(
                    item.index
                    for item in container.boundary_ports
                    if item.direction == direction
                )
                if indices != list(range(len(indices))):
                    raise PlacementError(
                        f"Physical container {container.path!r} has non-contiguous "
                        f"{direction.value} boundary indices"
                    )
            if Counter(item.net for item in container.connections) != Counter(
                item.net for item in topology.dependencies
            ):
                raise PlacementError(
                    f"Physical container {container.path!r} connection coverage differs"
                )
            for component in container.components:
                if component.provider != self.target:
                    raise PlacementError(
                        f"Physical component {component.identifier!r} targets "
                        f"{component.provider!r}, expected {self.target!r}"
                    )
            for annotation in container.annotations:
                if annotation.provider != self.target:
                    raise PlacementError(
                        f"Physical annotation {annotation.identifier!r} targets "
                        f"{annotation.provider!r}, expected {self.target!r}"
                    )
            for connection in container.connections:
                _validate_endpoint(
                    connection.source,
                    connection.net,
                    source=True,
                    container=container,
                    containers=by_path,
                )
                _validate_endpoint(
                    connection.target,
                    connection.net,
                    source=False,
                    container=container,
                    containers=by_path,
                )
        object.__setattr__(self, "containers", containers)

    def container(self, path: str) -> PhysicalContainer:
        try:
            return next(item for item in self.containers if item.path == path)
        except StopIteration as error:
            raise PlacementError(f"Unknown physical container {path!r}") from error


def _require_unique(values, context: str) -> None:
    seen: set[object] = set()
    for value in values:
        if value in seen:
            raise PlacementError(f"Duplicate {context} {value!r}")
        seen.add(value)


def _validate_endpoint(
    endpoint: PhysicalEndpoint,
    net: MaterialNetId,
    *,
    source: bool,
    container: PhysicalContainer,
    containers: dict[str, PhysicalContainer],
) -> None:
    if endpoint.kind == PhysicalEndpointKind.COMPONENT:
        if endpoint.identifier not in {
            item.identifier for item in container.components
        }:
            raise PlacementError(
                f"Physical connection references missing component "
                f"{endpoint.identifier!r} in {container.path!r}"
            )
        return
    if endpoint.kind == PhysicalEndpointKind.BOUNDARY:
        direction = PortDirection.INPUT if source else PortDirection.OUTPUT
        if endpoint.identifier != container.path or not any(
            item.direction == direction
            and item.index == endpoint.port
            and item.net == net
            for item in container.boundary_ports
        ):
            raise PlacementError(
                f"Physical connection references missing {direction.value} "
                f"boundary {endpoint.port} in {container.path!r}"
            )
        return
    if endpoint.identifier not in container.children:
        raise PlacementError(
            f"Physical connection references missing child {endpoint.identifier!r} "
            f"in {container.path!r}"
        )
    child = containers[endpoint.identifier]
    direction = PortDirection.OUTPUT if source else PortDirection.INPUT
    if not any(
        item.direction == direction and item.net == net
        for item in child.boundary_ports
    ):
        raise PlacementError(
            f"Physical connection references missing {direction.value} net "
            f"{net.value} on child {child.path!r}"
        )