from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
import hashlib
import json
import math
import re
from typing import Protocol

from gateforge.behavior import BehaviorGraph
from gateforge.behavior_source import BehaviorCapture, MaterialBehaviorBoundary
from gateforge.graph import MaterialGraph
from gateforge.hierarchy import GeneratedHierarchyPolicy, PhysicalHierarchyPolicy
from gateforge.material import MaterialDesign, MaterialDesignDigest, MaterialObjectId
from gateforge.placement import (
    PlacedDesign,
    PlacementError,
    ScoreBreakdown,
    ScoreComponent,
    TopologicalPlacementOptions,
    TopologicalPlacer,
    validate_placed_design,
)
from gateforge.provider import TargetProvider
from gateforge.target import ProviderConfiguration


class RealizationError(ValueError):
    pass


class RealizationInfeasibleError(RealizationError):
    pass


@dataclass(frozen=True, slots=True, order=True)
class RealizedEndpoint:
    entity: str
    port: str

    def __post_init__(self) -> None:
        if not self.entity or not self.port:
            raise RealizationError("Realized endpoint names must not be empty")

    def canonical_data(self) -> dict[str, object]:
        return {"entity": self.entity, "port": self.port}


@dataclass(frozen=True, slots=True)
class RealizedEntity:
    identifier: str
    kind: str
    ports: tuple[str, ...]
    configuration: ProviderConfiguration = ProviderConfiguration()

    def __post_init__(self) -> None:
        if not self.identifier or not self.kind:
            raise RealizationError("Realized entity identifier and kind are required")
        if not isinstance(self.ports, tuple) or any(not port for port in self.ports):
            raise RealizationError("Realized ports must be an immutable tuple of names")
        if len(set(self.ports)) != len(self.ports):
            raise RealizationError("Duplicate realized entity port")

    def canonical_data(self) -> dict[str, object]:
        return {
            "identifier": self.identifier,
            "kind": self.kind,
            "ports": sorted(self.ports),
            "configuration": self.configuration.canonical_data(),
        }


@dataclass(frozen=True, slots=True)
class RealizationProvenance:
    rule: str
    entities: tuple[str, ...] = ()
    material_objects: tuple[MaterialObjectId, ...] = ()
    values: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not self.rule:
            raise RealizationError("Realization provenance requires a versioned rule name")
        for items in (self.entities, self.material_objects, self.values):
            if not isinstance(items, tuple) or len(set(items)) != len(items):
                raise RealizationError("Provenance references must be unique immutable tuples")

    def canonical_data(self) -> dict[str, object]:
        return {
            "rule": self.rule,
            "entities": sorted(self.entities),
            "material_objects": sorted(item.value for item in self.material_objects),
            "values": sorted(self.values),
        }


@dataclass(frozen=True, slots=True)
class RealizedObservation:
    name: str
    value: str
    endpoints: tuple[RealizedEndpoint, ...]

    def __post_init__(self) -> None:
        if not self.name or not self.value or not self.endpoints:
            raise RealizationError("Realized observations require a name, value and endpoints")
        if not isinstance(self.endpoints, tuple) or len(set(self.endpoints)) != len(self.endpoints):
            raise RealizationError("Observation endpoints must be unique immutable tuples")

    def canonical_data(self) -> dict[str, object]:
        return {
            "name": self.name,
            "value": self.value,
            "endpoints": [item.canonical_data() for item in sorted(self.endpoints)],
        }


@dataclass(frozen=True, slots=True)
class RealizationDesign:
    target: str
    material_digest: MaterialDesignDigest
    behavior_digest: str
    entities: tuple[RealizedEntity, ...]
    observations: tuple[RealizedObservation, ...]
    provenance: tuple[RealizationProvenance, ...] = ()

    def __post_init__(self) -> None:
        if not self.target:
            raise RealizationError("Realization target is required")
        for digest in (self.material_digest.value, self.behavior_digest):
            if re.fullmatch(r"[0-9a-f]{64}", digest) is None:
                raise RealizationError("Realization references require SHA-256 digests")
        for items in (self.entities, self.observations, self.provenance):
            if not isinstance(items, tuple):
                raise RealizationError("Realization collections must be immutable tuples")
        entities = {item.identifier: item for item in self.entities}
        if len(entities) != len(self.entities):
            raise RealizationError("Duplicate realized entity")
        if len({item.name for item in self.observations}) != len(self.observations):
            raise RealizationError("Duplicate realized observation")
        endpoints = {
            RealizedEndpoint(entity.identifier, port)
            for entity in self.entities for port in entity.ports
        }
        for observation in self.observations:
            if not set(observation.endpoints) <= endpoints:
                raise RealizationError("Observation references an unknown realized endpoint")
        for origin in self.provenance:
            if not set(origin.entities) <= entities.keys():
                raise RealizationError("Provenance references an unknown realized entity")

    def canonical_data(self) -> dict[str, object]:
        return {
            "schema_version": 1,
            "kind": "realization-design",
            "target": self.target,
            "material_digest": self.material_digest.value,
            "behavior_digest": self.behavior_digest,
            "entities": [item.canonical_data() for item in sorted(
                self.entities, key=lambda item: item.identifier
            )],
            "observations": [item.canonical_data() for item in sorted(
                self.observations, key=lambda item: item.name
            )],
            "provenance": sorted(
                (item.canonical_data() for item in self.provenance),
                key=lambda item: json.dumps(item, sort_keys=True),
            ),
        }

    def get_digest(self) -> str:
        return hashlib.sha256(json.dumps(
            self.canonical_data(), sort_keys=True, separators=(",", ":"), allow_nan=False
        ).encode("utf-8")).hexdigest()

    def validate(self, material: MaterialDesign, behavior: BehaviorGraph) -> None:
        if self.material_digest != material.get_digest():
            raise RealizationError("Realization material digest mismatch")
        if self.behavior_digest != behavior.get_digest():
            raise RealizationError("Realization behavior digest mismatch")
        values = {item.identifier for item in behavior.nodes}
        material_objects = {item.identifier for item in material.objects}
        for origin in self.provenance:
            if not set(origin.material_objects) <= material_objects:
                raise RealizationError("Provenance references an unknown material object")
            if not set(origin.values) <= values:
                raise RealizationError("Provenance references an unknown behavior value")
        expected = {item.name: item.value for item in behavior.observations}
        if {item.name: item.value for item in self.observations} != expected:
            raise RealizationError("Realization observations do not match source behavior")


@dataclass(frozen=True, slots=True)
class RealizedEntityPlacement:
    entity: str
    x: float
    y: float
    angle: float = 0.0

    def __post_init__(self) -> None:
        if not self.entity:
            raise RealizationError("Placed entity requires an identifier")
        for attribute in ("x", "y", "angle"):
            value = getattr(self, attribute)
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
                raise RealizationError("Realized placement coordinates must be finite numbers")
            object.__setattr__(self, attribute, float(value))

    def canonical_data(self) -> dict[str, object]:
        return {"entity": self.entity, "x": self.x, "y": self.y, "angle": self.angle}


@dataclass(frozen=True, slots=True)
class RealizedPlacement:
    realization_digest: str
    entities: tuple[RealizedEntityPlacement, ...]

    def __post_init__(self) -> None:
        if re.fullmatch(r"[0-9a-f]{64}", self.realization_digest) is None:
            raise RealizationError("Realized placement requires an inventory SHA-256 digest")
        if not isinstance(self.entities, tuple):
            raise RealizationError("Realized placements must be an immutable tuple")
        if len({item.entity for item in self.entities}) != len(self.entities):
            raise RealizationError("Duplicate realized entity placement")

    def validate(self, design: RealizationDesign) -> None:
        if self.realization_digest != design.get_digest():
            raise RealizationError("Realized placement inventory digest mismatch")
        if {item.entity for item in self.entities} != {item.identifier for item in design.entities}:
            raise RealizationError("Realized placement must cover exactly its inventory")

    def canonical_data(self) -> dict[str, object]:
        return {
            "schema_version": 1,
            "kind": "realized-placement",
            "realization_digest": self.realization_digest,
            "entities": [item.canonical_data() for item in sorted(self.entities, key=lambda item: item.entity)],
        }


@dataclass(frozen=True, slots=True)
class RealizationProblem:
    material: MaterialDesign
    providers: Mapping[str, TargetProvider]
    behavior: BehaviorCapture | None = None
    boundary: MaterialBehaviorBoundary | None = None


@dataclass(frozen=True, slots=True)
class RealizationOptions:
    placement: TopologicalPlacementOptions = TopologicalPlacementOptions()
    physical_hierarchy: PhysicalHierarchyPolicy = PhysicalHierarchyPolicy()
    generated_hierarchy: GeneratedHierarchyPolicy = GeneratedHierarchyPolicy()
    provider_options: ProviderConfiguration = ProviderConfiguration()


class RealizationArtifact(Protocol):
    @property
    def target(self) -> str: ...

    def canonical_data(self) -> dict[str, object]: ...

    def validate(
        self,
        material: MaterialDesign,
        providers: Mapping[str, TargetProvider],
    ) -> None: ...


@dataclass(frozen=True, slots=True)
class RealizationOutcome:
    artifact: RealizationArtifact
    score: ScoreBreakdown

    def __post_init__(self) -> None:
        if any(
            component.value < 0 or component.weight < 0
            for component in self.score.components
        ):
            raise RealizationError("Realization costs and weights must be nonnegative")
        if not math.isfinite(self.score.total):
            raise RealizationInfeasibleError("Realization score must be finite")

    def artifact_digest(self) -> str:
        encoded = json.dumps(
            self.artifact.canonical_data(),
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()


class RealizationStrategy(Protocol):
    def realize(
        self,
        material: MaterialDesign,
        providers: Mapping[str, TargetProvider],
        options: RealizationOptions,
    ) -> Iterable[RealizationOutcome]: ...


class SourceRealizationStrategy(Protocol):
    def realize_problem(
        self, problem: RealizationProblem, options: RealizationOptions,
    ) -> Iterable[RealizationOutcome]: ...


class RealizationPresenter(Protocol):
    def placement(self, artifact: RealizationArtifact) -> RealizedPlacement: ...

    def normalize_options(self, options: Mapping[str, object]) -> ProviderConfiguration: ...

    def views(self, artifact: RealizationArtifact) -> tuple: ...

    def export(self, artifact: RealizationArtifact, *, label: str | None = None) -> dict[str, object]: ...

    def details(self, artifact: RealizationArtifact) -> dict[str, object]: ...


@dataclass(frozen=True, slots=True)
class DirectMaterialArtifact:
    placement: PlacedDesign

    @property
    def target(self) -> str:
        return self.placement.target

    def canonical_data(self) -> dict[str, object]:
        return {
            "schema_version": 1,
            "kind": "direct-material",
            "placement": self.placement.canonical_data(),
        }

    def validate(
        self,
        material: MaterialDesign,
        providers: Mapping[str, TargetProvider],
    ) -> None:
        validate_placed_design(
            self.placement, MaterialGraph.from_design(material, providers)
        )


@dataclass(frozen=True, slots=True)
class DirectMaterialRealizationStrategy:
    def realize_problem(
        self, problem: RealizationProblem, options: RealizationOptions,
    ) -> tuple[RealizationOutcome, ...]:
        return self.realize(problem.material, problem.providers, options)

    def realize(
        self,
        material: MaterialDesign,
        providers: Mapping[str, TargetProvider],
        options: RealizationOptions,
    ) -> tuple[RealizationOutcome, ...]:
        graph = MaterialGraph.from_design(material, providers)
        try:
            placement = TopologicalPlacer(
                options.placement,
                providers=providers,
                physical_hierarchy=options.physical_hierarchy,
                generated_hierarchy=options.generated_hierarchy,
            ).place(graph).finalize(graph)
        except PlacementError as error:
            raise RealizationInfeasibleError(str(error)) from error
        object_cost = sum(
            providers[item.type.provider].material_object_cost(item)
            for item in material.objects
        )
        return (
            RealizationOutcome(
                DirectMaterialArtifact(placement),
                ScoreBreakdown((ScoreComponent("provider_object_cost", object_cost),)),
            ),
        )