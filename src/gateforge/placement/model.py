from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
import math
import re

from gateforge.graph import (
    ConstantSubject,
    MaterialGraph,
    MaterialSubject,
    ModulePortSubject,
    ObjectSubject,
)
from gateforge.material import MaterialDesignDigest, MaterialNetId, MaterialObjectId
from gateforge.target import PortDirection, ProviderConfiguration


PLACEMENT_SCHEMA_VERSION = 1
_SHA256 = re.compile(r"[0-9a-f]{64}")


class PlacementError(ValueError):
    pass


class ContainerKind(StrEnum):
    MODULE = "module"
    IMPLEMENTATION = "implementation"


@dataclass(frozen=True, slots=True)
class PhysicalBounds:
    min_x: float
    min_y: float
    max_x: float
    max_y: float

    def __post_init__(self) -> None:
        for attribute in ("min_x", "min_y", "max_x", "max_y"):
            value = getattr(self, attribute)
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise PlacementError(f"Physical bound {attribute} must be numeric")
            normalized = float(value)
            if not math.isfinite(normalized):
                raise PlacementError(f"Physical bound {attribute} must be finite")
            object.__setattr__(self, attribute, normalized)
        if self.max_x < self.min_x or self.max_y < self.min_y:
            raise PlacementError("Physical bounds must have nonnegative dimensions")

    @classmethod
    def centered(cls, width: float, height: float) -> "PhysicalBounds":
        width = _require_nonnegative_float(width, "physical width")
        height = _require_nonnegative_float(height, "physical height")
        return cls(-width / 2, -height / 2, width / 2, height / 2)

    @property
    def width(self) -> float:
        return self.max_x - self.min_x

    @property
    def height(self) -> float:
        return self.max_y - self.min_y

    def canonical_data(self) -> dict[str, float]:
        return {
            "min_x": self.min_x,
            "min_y": self.min_y,
            "max_x": self.max_x,
            "max_y": self.max_y,
        }


@dataclass(frozen=True, slots=True)
class PlacementOptions:
    canonical_json: str = "{}"

    def __post_init__(self) -> None:
        configuration = ProviderConfiguration(self.canonical_json)
        object.__setattr__(self, "canonical_json", configuration.canonical_json)

    @classmethod
    def from_canonical_data(cls, value: object) -> "PlacementOptions":
        return cls(ProviderConfiguration.from_canonical_data(value).canonical_json)

    def canonical_data(self) -> dict[str, object]:
        return ProviderConfiguration(self.canonical_json).canonical_data()


class PhysicalEndpointKind(StrEnum):
    COMPONENT = "component"
    BOUNDARY = "boundary"
    CHILD = "child"


@dataclass(frozen=True, slots=True, order=True)
class PhysicalEndpoint:
    kind: PhysicalEndpointKind
    identifier: str
    port: int

    def __post_init__(self) -> None:
        if not self.identifier:
            raise PlacementError("Physical endpoint ID must not be empty")
        if (
            not isinstance(self.port, int)
            or isinstance(self.port, bool)
            or self.port < 0
        ):
            raise PlacementError("Physical endpoint port must be nonnegative")


@dataclass(frozen=True, slots=True, order=True)
class PhysicalConnection:
    net: MaterialNetId
    source: PhysicalEndpoint
    target: PhysicalEndpoint


@dataclass(frozen=True, slots=True)
class BoundaryPortPlacement:
    net: MaterialNetId
    direction: PortDirection
    index: int
    x: float
    y: float
    angle: float = 0.0

    def __post_init__(self) -> None:
        if self.index < 0:
            raise PlacementError("Boundary-port placement index must be nonnegative")
        _normalize_transform(self)


@dataclass(frozen=True, slots=True)
class AnnotationPlacement:
    identifier: str
    text: str
    x: float
    y: float
    angle: float = 0.0
    scale_x: float = 1.0
    scale_y: float = 1.0
    provider: str = ""
    bounds: PhysicalBounds = PhysicalBounds(0.0, 0.0, 0.0, 0.0)
    payload: ProviderConfiguration = ProviderConfiguration()

    def __post_init__(self) -> None:
        if not self.identifier:
            raise PlacementError("Annotation placement ID must not be empty")
        if not self.text:
            raise PlacementError("Annotation text must not be empty")
        _normalize_transform(self)
        for attribute in ("scale_x", "scale_y"):
            value = _require_positive_float(
                getattr(self, attribute),
                f"annotation {attribute}",
            )
            object.__setattr__(self, attribute, value)


@dataclass(frozen=True, slots=True)
class ComponentPlacement:
    identifier: str
    provider: str
    kind: str
    source: MaterialSubject
    bounds: PhysicalBounds
    payload: ProviderConfiguration
    x: float
    y: float
    angle: float = 0.0

    def __post_init__(self) -> None:
        if not self.identifier or not self.provider or not self.kind:
            raise PlacementError(
                "Placed component ID, provider, and kind must not be empty"
            )
        _normalize_transform(self)


@dataclass(frozen=True, slots=True)
class PrefabPlacement:
    identifier: str
    source: MaterialSubject
    bounds: PhysicalBounds
    x: float
    y: float
    angle: float
    components: tuple[str, ...]
    annotations: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not self.identifier:
            raise PlacementError("Placed prefab ID must not be empty")
        _normalize_transform(self)
        components = tuple(sorted(self.components))
        annotations = tuple(sorted(self.annotations))
        if not components:
            raise PlacementError("Placed prefab requires a component")
        _require_unique(components, "placed prefab component")
        _require_unique(annotations, "placed prefab annotation")
        object.__setattr__(self, "components", components)
        object.__setattr__(self, "annotations", annotations)


@dataclass(frozen=True, slots=True)
class PlacedContainer:
    path: str
    kind: ContainerKind
    name: str
    parent: str | None
    x: float
    y: float
    angle: float
    board: PhysicalBounds
    facade: PhysicalBounds
    components: tuple[ComponentPlacement, ...] = ()
    boundary_ports: tuple[BoundaryPortPlacement, ...] = ()
    annotations: tuple[AnnotationPlacement, ...] = ()
    prefabs: tuple[PrefabPlacement, ...] = ()
    connections: tuple[PhysicalConnection, ...] = ()
    children: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not self.path or not self.name:
            raise PlacementError("Placed container path and name must not be empty")
        _normalize_transform(self)
        components = tuple(
            sorted(self.components, key=lambda item: item.identifier)
        )
        boundary_ports = tuple(
            sorted(
                self.boundary_ports,
                key=lambda item: (item.direction.value, item.index, item.net.value),
            )
        )
        annotations = tuple(
            sorted(self.annotations, key=lambda item: item.identifier)
        )
        prefabs = tuple(sorted(self.prefabs, key=lambda item: item.identifier))
        connections = tuple(sorted(set(self.connections)))
        children = tuple(sorted(self.children))
        _require_unique(
            (item.identifier for item in components),
            "component placement",
        )
        _require_unique(
            ((item.direction, item.index) for item in boundary_ports),
            "boundary-port index",
        )
        _require_unique(
            (item.identifier for item in annotations),
            "annotation placement",
        )
        _require_unique(
            (item.identifier for item in prefabs),
            "prefab placement",
        )
        _require_unique(children, "child container")
        component_ids = {item.identifier for item in components}
        annotation_ids = {item.identifier for item in annotations}
        prefab_component_references = tuple(
            identifier for prefab in prefabs for identifier in prefab.components
        )
        prefab_annotation_references = tuple(
            identifier for prefab in prefabs for identifier in prefab.annotations
        )
        if len(set(prefab_component_references)) != len(
            prefab_component_references
        ):
            raise PlacementError(
                f"Placed container {self.path!r} has a component in multiple prefabs"
            )
        if len(set(prefab_annotation_references)) != len(
            prefab_annotation_references
        ):
            raise PlacementError(
                f"Placed container {self.path!r} has an annotation in multiple prefabs"
            )
        prefab_component_ids = set(prefab_component_references)
        prefab_annotation_ids = set(prefab_annotation_references)
        if prefab_component_ids != component_ids:
            raise PlacementError(
                f"Placed container {self.path!r} prefab/component coverage differs"
            )
        if prefab_annotation_ids != annotation_ids:
            raise PlacementError(
                f"Placed container {self.path!r} prefab/annotation coverage differs"
            )
        components_by_id = {item.identifier: item for item in components}
        annotations_by_id = {item.identifier: item for item in annotations}
        for prefab in prefabs:
            prefab_bounds = _transformed_bounds(
                prefab.bounds,
                prefab.x,
                prefab.y,
                prefab.angle,
            )
            _require_contained_bounds(
                prefab_bounds,
                self.board,
                f"Prefab {prefab.identifier!r} is outside board {self.path!r}",
            )
            for identifier in prefab.components:
                component = components_by_id[identifier]
                if component.source != prefab.source:
                    raise PlacementError(
                        f"Component {identifier!r} source differs from its prefab"
                    )
                _require_contained_bounds(
                    _transformed_bounds(
                        component.bounds,
                        component.x,
                        component.y,
                        component.angle,
                    ),
                    prefab_bounds,
                    f"Component {identifier!r} is outside prefab {prefab.identifier!r}",
                )
            for identifier in prefab.annotations:
                annotation = annotations_by_id[identifier]
                _require_contained_bounds(
                    _transformed_bounds(
                        annotation.bounds,
                        annotation.x,
                        annotation.y,
                        annotation.angle,
                    ),
                    prefab_bounds,
                    f"Annotation {identifier!r} is outside prefab {prefab.identifier!r}",
                )
        for boundary in boundary_ports:
            if not _contains_point(self.board, boundary.x, boundary.y):
                raise PlacementError(
                    f"Boundary port {boundary.direction.value} {boundary.index} is "
                    f"outside board {self.path!r}"
                )
        object.__setattr__(self, "components", components)
        object.__setattr__(self, "boundary_ports", boundary_ports)
        object.__setattr__(self, "annotations", annotations)
        object.__setattr__(self, "prefabs", prefabs)
        object.__setattr__(self, "connections", connections)
        object.__setattr__(self, "children", children)

    def canonical_data(self) -> dict[str, object]:
        return {
            "path": self.path,
            "kind": self.kind.value,
            "name": self.name,
            "parent": self.parent,
            "x": self.x,
            "y": self.y,
            "angle": self.angle,
            "board": self.board.canonical_data(),
            "facade": self.facade.canonical_data(),
            "components": [
                _component_placement_data(item) for item in self.components
            ],
            "boundary_ports": [
                {
                    "net": item.net.value,
                    "direction": item.direction.value,
                    "index": item.index,
                    "x": item.x,
                    "y": item.y,
                    "angle": item.angle,
                }
                for item in self.boundary_ports
            ],
            "annotations": [
                {
                    "id": item.identifier,
                    "text": item.text,
                    "x": item.x,
                    "y": item.y,
                    "angle": item.angle,
                    "scale_x": item.scale_x,
                    "scale_y": item.scale_y,
                    "provider": item.provider,
                    "bounds": item.bounds.canonical_data(),
                    "payload": item.payload.canonical_data(),
                }
                for item in self.annotations
            ],
            "prefabs": [_prefab_placement_data(item) for item in self.prefabs],
            "connections": [
                _physical_connection_data(item) for item in self.connections
            ],
            "children": list(self.children),
        }


@dataclass(frozen=True, slots=True)
class PlacementProvenance:
    placer: str
    version: int
    options: PlacementOptions = PlacementOptions()

    def __post_init__(self) -> None:
        if not self.placer:
            raise PlacementError("Placer name must not be empty")
        if (
            not isinstance(self.version, int)
            or isinstance(self.version, bool)
            or self.version <= 0
        ):
            raise PlacementError("Placer version must be a positive integer")


@dataclass(frozen=True, slots=True)
class PlacedDesign:
    material_digest: MaterialDesignDigest
    target: str
    provenance: PlacementProvenance
    root: str
    containers: tuple[PlacedContainer, ...]

    def __post_init__(self) -> None:
        if not self.target or not self.root:
            raise PlacementError("Placed design target and root must not be empty")
        containers = tuple(sorted(self.containers, key=lambda item: item.path))
        _require_unique((item.path for item in containers), "placed container")
        if self.root not in {item.path for item in containers}:
            raise PlacementError("Placed design root container is missing")
        object.__setattr__(self, "containers", containers)

    @property
    def root_container(self) -> PlacedContainer:
        return self.container(self.root)

    def container(self, path: str) -> PlacedContainer:
        try:
            return next(item for item in self.containers if item.path == path)
        except StopIteration as error:
            raise PlacementError(f"Unknown placed container {path!r}") from error

    @property
    def bounds(self) -> PhysicalBounds:
        return self.root_container.board

    @property
    def components(self) -> tuple[ComponentPlacement, ...]:
        result: list[ComponentPlacement] = []
        by_path = {item.path: item for item in self.containers}

        def visit(path: str, parent_x: float, parent_y: float, parent_angle: float) -> None:
            container = by_path[path]
            origin_x, origin_y = _apply_transform(
                container.x,
                container.y,
                parent_x,
                parent_y,
                parent_angle,
            )
            angle = parent_angle + container.angle
            for item in container.components:
                x, y = _apply_transform(item.x, item.y, origin_x, origin_y, angle)
                result.append(
                    ComponentPlacement(
                        item.identifier,
                        item.provider,
                        item.kind,
                        item.source,
                        item.bounds,
                        item.payload,
                        x,
                        y,
                        angle + item.angle,
                    )
                )
            for child in container.children:
                visit(child, origin_x, origin_y, angle)

        visit(self.root, 0.0, 0.0, 0.0)
        return tuple(sorted(result, key=lambda item: item.identifier))

    @property
    def prefabs(self) -> tuple[PrefabPlacement, ...]:
        result: list[PrefabPlacement] = []
        by_path = {item.path: item for item in self.containers}

        def visit(path: str, parent_x: float, parent_y: float, parent_angle: float) -> None:
            container = by_path[path]
            origin_x, origin_y = _apply_transform(
                container.x,
                container.y,
                parent_x,
                parent_y,
                parent_angle,
            )
            angle = parent_angle + container.angle
            for item in container.prefabs:
                x, y = _apply_transform(item.x, item.y, origin_x, origin_y, angle)
                result.append(
                    PrefabPlacement(
                        item.identifier,
                        item.source,
                        item.bounds,
                        x,
                        y,
                        angle + item.angle,
                        item.components,
                        item.annotations,
                    )
                )
            for child in container.children:
                visit(child, origin_x, origin_y, angle)

        visit(self.root, 0.0, 0.0, 0.0)
        return tuple(sorted(result, key=lambda item: item.identifier))

    @property
    def subjects(self) -> tuple[MaterialSubject, ...]:
        return tuple(item.source for item in self.prefabs)

    @property
    def annotations(self) -> tuple[AnnotationPlacement, ...]:
        result: list[AnnotationPlacement] = []
        by_path = {item.path: item for item in self.containers}

        def visit(path: str, parent_x: float, parent_y: float, parent_angle: float) -> None:
            container = by_path[path]
            origin_x, origin_y = _apply_transform(
                container.x,
                container.y,
                parent_x,
                parent_y,
                parent_angle,
            )
            angle = parent_angle + container.angle
            for item in container.annotations:
                x, y = _apply_transform(item.x, item.y, origin_x, origin_y, angle)
                result.append(
                    AnnotationPlacement(
                        item.identifier,
                        item.text,
                        x,
                        y,
                        angle + item.angle,
                        item.scale_x,
                        item.scale_y,
                        item.provider,
                        item.bounds,
                        item.payload,
                    )
                )
            for child in container.children:
                visit(child, origin_x, origin_y, angle)

        visit(self.root, 0.0, 0.0, 0.0)
        return tuple(sorted(result, key=lambda item: item.identifier))

    def canonical_data(self) -> dict[str, object]:
        return {
            "schema_version": PLACEMENT_SCHEMA_VERSION,
            "material_digest": self.material_digest.value,
            "target": self.target,
            "placer": self.provenance.placer,
            "placer_version": self.provenance.version,
            "options": self.provenance.options.canonical_data(),
            "root": self.root,
            "containers": [item.canonical_data() for item in self.containers],
        }

    @classmethod
    def from_canonical_data(
        cls,
        value: object,
        graph: MaterialGraph,
    ) -> "PlacedDesign":
        data = _require_mapping(value, "placed design")
        if {"objects", "module_ports", "constants"}.issubset(data):
            raise PlacementError(
                "Flat placement schema v1 is obsolete; regenerate the placement"
            )
        _require_keys(
            data,
            {
                "schema_version",
                "material_digest",
                "target",
                "placer",
                "placer_version",
                "options",
                "root",
                "containers",
            },
            "placed design",
        )
        version = _require_int(data.get("schema_version"), "placement schema version")
        if version != PLACEMENT_SCHEMA_VERSION:
            raise PlacementError(f"Unsupported placement schema version {version}")
        material_digest = MaterialDesignDigest(
            _require_digest(data.get("material_digest"), "material digest")
        )
        target = _require_nonempty_str(data.get("target"), "placement target")
        provenance = PlacementProvenance(
            placer=_require_nonempty_str(data.get("placer"), "placer name"),
            version=_require_int(data.get("placer_version"), "placer version"),
            options=PlacementOptions.from_canonical_data(data.get("options")),
        )
        placed = cls(
            material_digest,
            target,
            provenance,
            _require_nonempty_str(data.get("root"), "placement root"),
            tuple(
                _decode_container(item)
                for item in _require_list(data.get("containers"), "placed containers")
            ),
        )
        validate_placed_design(placed, graph)
        return placed


class PlacementProposal(ABC):
    @abstractmethod
    def material_digest(self) -> MaterialDesignDigest:
        raise NotImplementedError

    @abstractmethod
    def resolve(self, graph: MaterialGraph) -> PlacedDesign:
        raise NotImplementedError

    def is_complete(self, graph: MaterialGraph) -> bool:
        try:
            validate_placed_design(self.resolve(graph), graph)
        except PlacementError:
            return False
        return True

    def finalize(self, graph: MaterialGraph) -> PlacedDesign:
        placed = self.resolve(graph)
        validate_placed_design(placed, graph)
        return placed


@dataclass(frozen=True, slots=True)
class ResolvedPlacementProposal(PlacementProposal):
    placed: PlacedDesign

    def material_digest(self) -> MaterialDesignDigest:
        return self.placed.material_digest

    def resolve(self, graph: MaterialGraph) -> PlacedDesign:
        if graph.design.get_digest() != self.placed.material_digest:
            raise PlacementError("Placement proposal material digest does not match graph")
        return self.placed


class Placer(ABC):
    @abstractmethod
    def place(self, graph: MaterialGraph) -> PlacementProposal:
        raise NotImplementedError


def validate_placed_design(placed: PlacedDesign, graph: MaterialGraph) -> None:
    expected_digest = graph.design.get_digest()
    if placed.material_digest != expected_digest:
        raise PlacementError(
            f"Placement references material digest {placed.material_digest.value}, "
            f"expected {expected_digest.value}"
        )
    by_path = {item.path: item for item in placed.containers}
    root = by_path[placed.root]
    if root.parent is not None:
        raise PlacementError("Placed design root container must not have a parent")
    if (root.x, root.y, root.angle) != (0.0, 0.0, 0.0):
        raise PlacementError("Placed design root container must use the origin transform")
    referenced_children: set[str] = set()
    for container in placed.containers:
        for child in container.children:
            if child not in by_path:
                raise PlacementError(
                    f"Placed container {container.path!r} has missing child {child!r}"
                )
            if by_path[child].parent != container.path:
                raise PlacementError(
                    f"Placed container {child!r} has inconsistent parent"
                )
            if child in referenced_children:
                raise PlacementError(f"Placed container {child!r} has multiple parents")
            referenced_children.add(child)
    expected_children = set(by_path) - {placed.root}
    if referenced_children != expected_children:
        raise PlacementError("Placed container tree is disconnected")
    reachable: set[str] = set()
    pending = [placed.root]
    while pending:
        path = pending.pop()
        if path in reachable:
            raise PlacementError(f"Placed container tree contains a cycle at {path!r}")
        reachable.add(path)
        pending.extend(by_path[path].children)
    if reachable != set(by_path):
        raise PlacementError("Placed container tree is disconnected")
    _require_unique(
        (
            component.identifier
            for container in placed.containers
            for component in container.components
        ),
        "global component placement",
    )
    _require_unique(
        (
            annotation.identifier
            for container in placed.containers
            for annotation in container.annotations
        ),
        "global annotation placement",
    )
    _require_unique(
        (
            prefab.identifier
            for container in placed.containers
            for prefab in container.prefabs
        ),
        "global prefab placement",
    )
    material_nets = {item.identifier for item in graph.design.nets}
    for container in placed.containers:
        for component in container.components:
            if component.provider != placed.target:
                raise PlacementError(
                    f"Component {component.identifier!r} targets "
                    f"{component.provider!r}, expected {placed.target!r}"
                )
        for annotation in container.annotations:
            if annotation.provider != placed.target:
                raise PlacementError(
                    f"Annotation {annotation.identifier!r} targets "
                    f"{annotation.provider!r}, expected {placed.target!r}"
                )
        for child_path in container.children:
            child = by_path[child_path]
            _require_contained_bounds(
                _transformed_bounds(
                    child.facade,
                    child.x,
                    child.y,
                    child.angle,
                ),
                container.board,
                f"Child facade {child.path!r} is outside board {container.path!r}",
            )
        for connection in container.connections:
            if connection.net not in material_nets:
                raise PlacementError(
                    f"Physical connection references unknown net "
                    f"{connection.net.value}"
                )
            _validate_physical_endpoint(
                connection.source,
                connection.net,
                source=True,
                container=container,
                containers=by_path,
            )
            _validate_physical_endpoint(
                connection.target,
                connection.net,
                source=False,
                container=container,
                containers=by_path,
            )
    placed_subjects = placed.subjects
    if len(set(placed_subjects)) != len(placed_subjects):
        raise PlacementError("Placement contains duplicate material subjects")
    actual = frozenset(placed_subjects)
    expected = frozenset(graph.subjects)
    missing = expected - actual
    unknown = actual - expected
    if missing or unknown:
        raise PlacementError(
            f"Placement subject coverage mismatch; missing={sorted(missing, key=repr)!r}, "
            f"unknown={sorted(unknown, key=repr)!r}"
        )


def _validate_physical_endpoint(
    endpoint: PhysicalEndpoint,
    net: MaterialNetId,
    *,
    source: bool,
    container: PlacedContainer,
    containers: Mapping[str, PlacedContainer],
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
        if endpoint.identifier != container.path:
            raise PlacementError(
                f"Physical connection references boundary for "
                f"{endpoint.identifier!r} from {container.path!r}"
            )
        direction = PortDirection.INPUT if source else PortDirection.OUTPUT
        boundary = next(
            (
                item
                for item in container.boundary_ports
                if item.direction == direction and item.index == endpoint.port
            ),
            None,
        )
        if boundary is None or boundary.net != net:
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


def _transformed_bounds(
    bounds: PhysicalBounds,
    x: float,
    y: float,
    angle: float,
) -> PhysicalBounds:
    points = tuple(
        _apply_transform(point_x, point_y, x, y, angle)
        for point_x in (bounds.min_x, bounds.max_x)
        for point_y in (bounds.min_y, bounds.max_y)
    )
    return PhysicalBounds(
        min(point[0] for point in points),
        min(point[1] for point in points),
        max(point[0] for point in points),
        max(point[1] for point in points),
    )


def _require_contained_bounds(
    inner: PhysicalBounds,
    outer: PhysicalBounds,
    message: str,
) -> None:
    tolerance = 1e-9
    if (
        inner.min_x < outer.min_x - tolerance
        or inner.min_y < outer.min_y - tolerance
        or inner.max_x > outer.max_x + tolerance
        or inner.max_y > outer.max_y + tolerance
    ):
        raise PlacementError(message)


def _contains_point(bounds: PhysicalBounds, x: float, y: float) -> bool:
    tolerance = 1e-9
    return (
        bounds.min_x - tolerance <= x <= bounds.max_x + tolerance
        and bounds.min_y - tolerance <= y <= bounds.max_y + tolerance
    )


def _normalize_transform(value: object) -> None:
    for attribute in ("x", "y", "angle"):
        number = getattr(value, attribute)
        if isinstance(number, bool) or not isinstance(number, (int, float)):
            raise PlacementError(f"Placement {attribute} must be numeric")
        normalized = float(number)
        if not math.isfinite(normalized):
            raise PlacementError(f"Placement {attribute} must be finite")
        object.__setattr__(value, attribute, normalized)


def _apply_transform(
    x: float,
    y: float,
    parent_x: float,
    parent_y: float,
    parent_angle: float,
) -> tuple[float, float]:
    radians = math.radians(parent_angle)
    cosine = math.cos(radians)
    sine = math.sin(radians)
    return (
        parent_x + x * cosine - y * sine,
        parent_y + x * sine + y * cosine,
    )


def _require_unique(values, context: str) -> None:
    seen: set[object] = set()
    for value in values:
        if value in seen:
            raise PlacementError(f"Duplicate {context} for {value!r}")
        seen.add(value)


def _decode_container(value: object) -> PlacedContainer:
    data = _require_mapping(value, "placed container")
    _require_keys(
        data,
        {
            "path",
            "kind",
            "name",
            "parent",
            "x",
            "y",
            "angle",
            "board",
            "facade",
            "components",
            "boundary_ports",
            "annotations",
            "prefabs",
            "connections",
            "children",
        },
        "placed container",
    )
    parent_value = data.get("parent")
    if parent_value is not None and not isinstance(parent_value, str):
        raise PlacementError("Placed container parent must be a string or null")
    return PlacedContainer(
        path=_require_nonempty_str(data.get("path"), "placed container path"),
        kind=ContainerKind(
            _require_nonempty_str(data.get("kind"), "placed container kind")
        ),
        name=_require_nonempty_str(data.get("name"), "placed container name"),
        parent=parent_value,
        x=_require_float(data.get("x"), "placed container x"),
        y=_require_float(data.get("y"), "placed container y"),
        angle=_require_float(data.get("angle"), "placed container angle"),
        board=_decode_bounds(data.get("board"), "placed container board"),
        facade=_decode_bounds(data.get("facade"), "placed container facade"),
        components=tuple(
            _decode_component_placement(item)
            for item in _require_list(data.get("components"), "component placements")
        ),
        boundary_ports=tuple(
            _decode_boundary_port_placement(item)
            for item in _require_list(
                data.get("boundary_ports"), "boundary-port placements"
            )
        ),
        annotations=tuple(
            _decode_annotation_placement(item)
            for item in _require_list(data.get("annotations"), "annotation placements")
        ),
        prefabs=tuple(
            _decode_prefab_placement(item)
            for item in _require_list(data.get("prefabs"), "prefab placements")
        ),
        connections=tuple(
            _decode_physical_connection(item)
            for item in _require_list(data.get("connections"), "physical connections")
        ),
        children=tuple(
            _require_nonempty_str(item, "child container path")
            for item in _require_list(data.get("children"), "child containers")
        ),
    )


def _decode_bounds(value: object, context: str) -> PhysicalBounds:
    data = _require_mapping(value, context)
    _require_keys(data, {"min_x", "min_y", "max_x", "max_y"}, context)
    return PhysicalBounds(
        _require_float(data.get("min_x"), f"{context} min_x"),
        _require_float(data.get("min_y"), f"{context} min_y"),
        _require_float(data.get("max_x"), f"{context} max_x"),
        _require_float(data.get("max_y"), f"{context} max_y"),
    )


def _decode_boundary_port_placement(value: object) -> BoundaryPortPlacement:
    data = _require_mapping(value, "boundary-port placement")
    _require_keys(
        data,
        {"net", "direction", "index", "x", "y", "angle"},
        "boundary-port placement",
    )
    return BoundaryPortPlacement(
        MaterialNetId(_require_digest(data.get("net"), "boundary-port net")),
        PortDirection(
            _require_nonempty_str(data.get("direction"), "boundary-port direction")
        ),
        _require_nonnegative_int(data.get("index"), "boundary-port index"),
        _require_float(data.get("x"), "boundary-port x"),
        _require_float(data.get("y"), "boundary-port y"),
        _require_float(data.get("angle"), "boundary-port angle"),
    )


def _decode_annotation_placement(value: object) -> AnnotationPlacement:
    data = _require_mapping(value, "annotation placement")
    _require_keys(
        data,
        {
            "id",
            "provider",
            "text",
            "bounds",
            "payload",
            "x",
            "y",
            "angle",
            "scale_x",
            "scale_y",
        },
        "annotation placement",
    )
    return AnnotationPlacement(
        _require_nonempty_str(data.get("id"), "annotation ID"),
        _require_nonempty_str(data.get("text"), "annotation text"),
        _require_float(data.get("x"), "annotation x"),
        _require_float(data.get("y"), "annotation y"),
        _require_float(data.get("angle"), "annotation angle"),
        _require_positive_float(data.get("scale_x"), "annotation scale_x"),
        _require_positive_float(data.get("scale_y"), "annotation scale_y"),
        _require_nonempty_str(data.get("provider"), "annotation provider"),
        _decode_bounds(data.get("bounds"), "annotation bounds"),
        ProviderConfiguration.from_canonical_data(data.get("payload")),
    )


def _decode_component_placement(value: object) -> ComponentPlacement:
    data = _require_mapping(value, "component placement")
    _require_keys(
        data,
        {
            "id",
            "provider",
            "kind",
            "source",
            "bounds",
            "payload",
            "x",
            "y",
            "angle",
        },
        "component placement",
    )
    return ComponentPlacement(
        _require_nonempty_str(data.get("id"), "component ID"),
        _require_nonempty_str(data.get("provider"), "component provider"),
        _require_nonempty_str(data.get("kind"), "component kind"),
        _decode_material_subject(data.get("source")),
        _decode_bounds(data.get("bounds"), "component bounds"),
        ProviderConfiguration.from_canonical_data(data.get("payload")),
        _require_float(data.get("x"), "component x"),
        _require_float(data.get("y"), "component y"),
        _require_float(data.get("angle"), "component angle"),
    )


def _decode_prefab_placement(value: object) -> PrefabPlacement:
    data = _require_mapping(value, "prefab placement")
    _require_keys(
        data,
        {
            "id",
            "source",
            "bounds",
            "x",
            "y",
            "angle",
            "components",
            "annotations",
        },
        "prefab placement",
    )
    return PrefabPlacement(
        _require_nonempty_str(data.get("id"), "prefab ID"),
        _decode_material_subject(data.get("source")),
        _decode_bounds(data.get("bounds"), "prefab bounds"),
        _require_float(data.get("x"), "prefab x"),
        _require_float(data.get("y"), "prefab y"),
        _require_float(data.get("angle"), "prefab angle"),
        tuple(
            _require_nonempty_str(item, "prefab component ID")
            for item in _require_list(data.get("components"), "prefab components")
        ),
        tuple(
            _require_nonempty_str(item, "prefab annotation ID")
            for item in _require_list(data.get("annotations"), "prefab annotations")
        ),
    )


def _decode_physical_connection(value: object) -> PhysicalConnection:
    data = _require_mapping(value, "physical connection")
    _require_keys(data, {"net", "source", "target"}, "physical connection")
    return PhysicalConnection(
        MaterialNetId(_require_digest(data.get("net"), "physical connection net")),
        _decode_physical_endpoint(data.get("source")),
        _decode_physical_endpoint(data.get("target")),
    )


def _decode_physical_endpoint(value: object) -> PhysicalEndpoint:
    data = _require_mapping(value, "physical endpoint")
    _require_keys(data, {"kind", "id", "port"}, "physical endpoint")
    return PhysicalEndpoint(
        PhysicalEndpointKind(
            _require_nonempty_str(data.get("kind"), "physical endpoint kind")
        ),
        _require_nonempty_str(data.get("id"), "physical endpoint ID"),
        _require_nonnegative_int(data.get("port"), "physical endpoint port"),
    )


def _decode_material_subject(value: object) -> MaterialSubject:
    data = _require_mapping(value, "material subject")
    kind = _require_nonempty_str(data.get("kind"), "material subject kind")
    if kind == "object":
        _require_keys(data, {"kind", "object"}, "object subject")
        return ObjectSubject(
            MaterialObjectId(_require_digest(data.get("object"), "subject object ID"))
        )
    if kind == "module_port":
        _require_keys(
            data,
            {"kind", "module", "port", "bit", "direction"},
            "module-port subject",
        )
        return ModulePortSubject(
            _require_nonempty_str(data.get("module"), "subject module"),
            _require_nonempty_str(data.get("port"), "subject port"),
            _require_nonnegative_int(data.get("bit"), "subject bit"),
            PortDirection(
                _require_nonempty_str(data.get("direction"), "subject direction")
            ),
        )
    if kind == "constant":
        _require_keys(data, {"kind", "net", "value"}, "constant subject")
        return ConstantSubject(
            MaterialNetId(_require_digest(data.get("net"), "subject net ID")),
            _require_nonempty_str(data.get("value"), "subject constant value"),
        )
    raise PlacementError(f"Unknown material subject kind {kind!r}")


def _component_placement_data(item: ComponentPlacement) -> dict[str, object]:
    return {
        "id": item.identifier,
        "provider": item.provider,
        "kind": item.kind,
        "source": _material_subject_data(item.source),
        "bounds": item.bounds.canonical_data(),
        "payload": item.payload.canonical_data(),
        "x": item.x,
        "y": item.y,
        "angle": item.angle,
    }


def _prefab_placement_data(item: PrefabPlacement) -> dict[str, object]:
    return {
        "id": item.identifier,
        "source": _material_subject_data(item.source),
        "bounds": item.bounds.canonical_data(),
        "x": item.x,
        "y": item.y,
        "angle": item.angle,
        "components": list(item.components),
        "annotations": list(item.annotations),
    }


def _physical_connection_data(item: PhysicalConnection) -> dict[str, object]:
    return {
        "net": item.net.value,
        "source": _physical_endpoint_data(item.source),
        "target": _physical_endpoint_data(item.target),
    }


def _physical_endpoint_data(item: PhysicalEndpoint) -> dict[str, object]:
    return {"kind": item.kind.value, "id": item.identifier, "port": item.port}


def _material_subject_data(item: MaterialSubject) -> dict[str, object]:
    if isinstance(item, ObjectSubject):
        return {"kind": "object", "object": item.object.value}
    if isinstance(item, ModulePortSubject):
        return {
            "kind": "module_port",
            "module": item.module,
            "port": item.port,
            "bit": item.bit,
            "direction": item.direction.value,
        }
    return {"kind": "constant", "net": item.net.value, "value": item.value}


def _require_mapping(value: object, context: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise PlacementError(f"{context} must be an object")
    return value


def _require_list(value: object, context: str) -> list[object]:
    if not isinstance(value, list):
        raise PlacementError(f"{context} must be a list")
    return value


def _require_nonempty_str(value: object, context: str) -> str:
    if not isinstance(value, str) or not value:
        raise PlacementError(f"{context} must be a nonempty string")
    return value


def _require_int(value: object, context: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool):
        raise PlacementError(f"{context} must be an integer")
    return value


def _require_nonnegative_int(value: object, context: str) -> int:
    result = _require_int(value, context)
    if result < 0:
        raise PlacementError(f"{context} must be nonnegative")
    return result


def _require_float(value: object, context: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise PlacementError(f"{context} must be numeric")
    result = float(value)
    if not math.isfinite(result):
        raise PlacementError(f"{context} must be finite")
    return result


def _require_nonnegative_float(value: object, context: str) -> float:
    result = _require_float(value, context)
    if result < 0:
        raise PlacementError(f"{context} must be nonnegative")
    return result


def _require_positive_float(value: object, context: str) -> float:
    result = _require_float(value, context)
    if result <= 0:
        raise PlacementError(f"{context} must be positive")
    return result


def _require_digest(value: object, context: str) -> str:
    result = _require_nonempty_str(value, context)
    if _SHA256.fullmatch(result) is None:
        raise PlacementError(f"{context} must be a lowercase SHA-256 digest")
    return result


def _require_keys(
    value: Mapping[str, object],
    expected: set[str],
    context: str,
) -> None:
    actual = set(value)
    missing = expected - actual
    unknown = actual - expected
    if missing:
        raise PlacementError(f"{context} is missing keys {sorted(missing)!r}")
    if unknown:
        raise PlacementError(f"{context} has unknown keys {sorted(unknown)!r}")
