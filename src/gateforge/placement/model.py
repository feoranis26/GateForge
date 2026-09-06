from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Mapping
from dataclasses import dataclass
import json
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


@dataclass(frozen=True, slots=True)
class ObjectPlacement:
    object: MaterialObjectId
    x: float
    y: float
    angle: float = 0.0

    def __post_init__(self) -> None:
        _normalize_transform(self)


@dataclass(frozen=True, slots=True)
class ModulePortPlacement:
    module: str
    port: str
    bit: int
    direction: PortDirection
    x: float
    y: float
    angle: float = 0.0

    def __post_init__(self) -> None:
        if not self.module or not self.port:
            raise PlacementError("Module-port placement names must not be empty")
        if self.bit < 0:
            raise PlacementError("Module-port placement bit must be nonnegative")
        _normalize_transform(self)


@dataclass(frozen=True, slots=True)
class ConstantPlacement:
    net: MaterialNetId
    value: str
    x: float
    y: float
    angle: float = 0.0

    def __post_init__(self) -> None:
        if not self.value:
            raise PlacementError("Constant placement value must not be empty")
        _normalize_transform(self)


type Placement = ObjectPlacement | ModulePortPlacement | ConstantPlacement


@dataclass(frozen=True, slots=True)
class PlacementBounds:
    min_x: float
    min_y: float
    max_x: float
    max_y: float


@dataclass(frozen=True, slots=True)
class ResolvedPlacements:
    objects: tuple[ObjectPlacement, ...]
    module_ports: tuple[ModulePortPlacement, ...]
    constants: tuple[ConstantPlacement, ...]

    def __post_init__(self) -> None:
        objects = tuple(sorted(self.objects, key=lambda item: item.object.value))
        module_ports = tuple(
            sorted(
                self.module_ports,
                key=lambda item: (
                    item.module,
                    item.port,
                    item.bit,
                    item.direction.value,
                ),
            )
        )
        constants = tuple(
            sorted(self.constants, key=lambda item: (item.net.value, item.value))
        )
        _require_unique(
            (item.object for item in objects),
            "object placement",
        )
        _require_unique(
            (
                (item.module, item.port, item.bit, item.direction)
                for item in module_ports
            ),
            "module-port placement",
        )
        _require_unique(
            ((item.net, item.value) for item in constants),
            "constant placement",
        )
        object.__setattr__(self, "objects", objects)
        object.__setattr__(self, "module_ports", module_ports)
        object.__setattr__(self, "constants", constants)

    @property
    def all(self) -> tuple[Placement, ...]:
        return (*self.objects, *self.module_ports, *self.constants)


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
    provenance: PlacementProvenance
    placements: ResolvedPlacements

    def __post_init__(self) -> None:
        if not self.placements.all:
            raise PlacementError("Placed design must contain at least one placement")

    @property
    def bounds(self) -> PlacementBounds:
        placements = self.placements.all
        return PlacementBounds(
            min(item.x for item in placements),
            min(item.y for item in placements),
            max(item.x for item in placements),
            max(item.y for item in placements),
        )

    def canonical_data(self) -> dict[str, object]:
        return {
            "schema_version": PLACEMENT_SCHEMA_VERSION,
            "material_digest": self.material_digest.value,
            "placer": self.provenance.placer,
            "placer_version": self.provenance.version,
            "options": self.provenance.options.canonical_data(),
            "objects": [
                {
                    "object": item.object.value,
                    "x": item.x,
                    "y": item.y,
                    "angle": item.angle,
                }
                for item in self.placements.objects
            ],
            "module_ports": [
                {
                    "module": item.module,
                    "port": item.port,
                    "bit": item.bit,
                    "direction": item.direction.value,
                    "x": item.x,
                    "y": item.y,
                    "angle": item.angle,
                }
                for item in self.placements.module_ports
            ],
            "constants": [
                {
                    "net": item.net.value,
                    "value": item.value,
                    "x": item.x,
                    "y": item.y,
                    "angle": item.angle,
                }
                for item in self.placements.constants
            ],
        }

    @classmethod
    def from_canonical_data(
        cls,
        value: object,
        graph: MaterialGraph,
    ) -> "PlacedDesign":
        data = _require_mapping(value, "placed design")
        _require_keys(
            data,
            {
                "schema_version",
                "material_digest",
                "placer",
                "placer_version",
                "options",
                "objects",
                "module_ports",
                "constants",
            },
            "placed design",
        )
        version = _require_int(data.get("schema_version"), "placement schema version")
        if version != PLACEMENT_SCHEMA_VERSION:
            raise PlacementError(f"Unsupported placement schema version {version}")
        material_digest = MaterialDesignDigest(
            _require_digest(data.get("material_digest"), "material digest")
        )
        provenance = PlacementProvenance(
            placer=_require_nonempty_str(data.get("placer"), "placer name"),
            version=_require_int(data.get("placer_version"), "placer version"),
            options=PlacementOptions.from_canonical_data(data.get("options")),
        )
        objects = tuple(
            _decode_object_placement(item)
            for item in _require_list(data.get("objects"), "object placements")
        )
        module_ports = tuple(
            _decode_module_port_placement(item)
            for item in _require_list(
                data.get("module_ports"), "module-port placements"
            )
        )
        constants = tuple(
            _decode_constant_placement(item)
            for item in _require_list(data.get("constants"), "constant placements")
        )
        placed = cls(
            material_digest,
            provenance,
            ResolvedPlacements(objects, module_ports, constants),
        )
        validate_placed_design(placed, graph)
        return placed


class PlacementProposal(ABC):
    @abstractmethod
    def material_digest(self) -> MaterialDesignDigest:
        raise NotImplementedError

    @abstractmethod
    def provenance(self) -> PlacementProvenance:
        raise NotImplementedError

    @abstractmethod
    def resolve(self, graph: MaterialGraph) -> ResolvedPlacements:
        raise NotImplementedError

    def is_complete(self, graph: MaterialGraph) -> bool:
        if self.material_digest() != graph.design.get_digest():
            return False
        return _placement_subjects(self.resolve(graph)) == frozenset(graph.subjects)

    def finalize(self, graph: MaterialGraph) -> PlacedDesign:
        placed = PlacedDesign(
            self.material_digest(),
            self.provenance(),
            self.resolve(graph),
        )
        validate_placed_design(placed, graph)
        return placed


@dataclass(frozen=True, slots=True)
class FlatPlacementProposal(PlacementProposal):
    source_digest: MaterialDesignDigest
    source_provenance: PlacementProvenance
    resolved: ResolvedPlacements

    def material_digest(self) -> MaterialDesignDigest:
        return self.source_digest

    def provenance(self) -> PlacementProvenance:
        return self.source_provenance

    def resolve(self, graph: MaterialGraph) -> ResolvedPlacements:
        if graph.design.get_digest() != self.source_digest:
            raise PlacementError("Placement proposal material digest does not match graph")
        return self.resolved


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
    actual = _placement_subjects(placed.placements)
    expected = frozenset(graph.subjects)
    missing = expected - actual
    unknown = actual - expected
    if missing or unknown:
        raise PlacementError(
            f"Placement subject coverage mismatch; missing={sorted(missing, key=repr)!r}, "
            f"unknown={sorted(unknown, key=repr)!r}"
        )


def _placement_subjects(placements: ResolvedPlacements) -> frozenset[MaterialSubject]:
    return frozenset(
        [ObjectSubject(item.object) for item in placements.objects]
        + [
            ModulePortSubject(item.module, item.port, item.bit, item.direction)
            for item in placements.module_ports
        ]
        + [ConstantSubject(item.net, item.value) for item in placements.constants]
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


def _require_unique(values, context: str) -> None:
    seen: set[object] = set()
    for value in values:
        if value in seen:
            raise PlacementError(f"Duplicate {context} for {value!r}")
        seen.add(value)


def _decode_object_placement(value: object) -> ObjectPlacement:
    data = _require_mapping(value, "object placement")
    _require_keys(data, {"object", "x", "y", "angle"}, "object placement")
    return ObjectPlacement(
        MaterialObjectId(_require_digest(data.get("object"), "placement object ID")),
        _require_float(data.get("x"), "placement x"),
        _require_float(data.get("y"), "placement y"),
        _require_float(data.get("angle"), "placement angle"),
    )


def _decode_module_port_placement(value: object) -> ModulePortPlacement:
    data = _require_mapping(value, "module-port placement")
    _require_keys(
        data,
        {"module", "port", "bit", "direction", "x", "y", "angle"},
        "module-port placement",
    )
    return ModulePortPlacement(
        module=_require_nonempty_str(data.get("module"), "placement module"),
        port=_require_nonempty_str(data.get("port"), "placement module port"),
        bit=_require_nonnegative_int(data.get("bit"), "placement module bit"),
        direction=PortDirection(
            _require_nonempty_str(data.get("direction"), "placement direction")
        ),
        x=_require_float(data.get("x"), "placement x"),
        y=_require_float(data.get("y"), "placement y"),
        angle=_require_float(data.get("angle"), "placement angle"),
    )


def _decode_constant_placement(value: object) -> ConstantPlacement:
    data = _require_mapping(value, "constant placement")
    _require_keys(
        data,
        {"net", "value", "x", "y", "angle"},
        "constant placement",
    )
    return ConstantPlacement(
        net=MaterialNetId(_require_digest(data.get("net"), "placement net ID")),
        value=_require_nonempty_str(data.get("value"), "placement constant"),
        x=_require_float(data.get("x"), "placement x"),
        y=_require_float(data.get("y"), "placement y"),
        angle=_require_float(data.get("angle"), "placement angle"),
    )


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
