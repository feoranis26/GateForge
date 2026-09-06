from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Iterable
from dataclasses import dataclass
import math

from gateforge.graph import MaterialGraph
from gateforge.material import MaterialDesignDigest
from gateforge.placement.model import PlacedDesign, PlacementProposal


class OptimizationError(ValueError):
    pass


class PhysicalDesignProposal(ABC):
    @abstractmethod
    def material_digest(self) -> MaterialDesignDigest:
        raise NotImplementedError

    @abstractmethod
    def resolve_placement(self, graph: MaterialGraph) -> PlacedDesign:
        raise NotImplementedError

    def validate(self, graph: MaterialGraph) -> None:
        if self.material_digest() != graph.design.get_digest():
            raise OptimizationError(
                "Physical proposal material digest does not match graph"
            )
        self.resolve_placement(graph)


@dataclass(frozen=True, slots=True)
class PlacementPhysicalDesignProposal(PhysicalDesignProposal):
    placement: PlacementProposal

    def material_digest(self) -> MaterialDesignDigest:
        return self.placement.material_digest()

    def resolve_placement(self, graph: MaterialGraph) -> PlacedDesign:
        return self.placement.finalize(graph)


@dataclass(frozen=True, slots=True)
class ScoreComponent:
    name: str
    value: float
    weight: float = 1.0

    def __post_init__(self) -> None:
        if not self.name:
            raise OptimizationError("Score component name must not be empty")
        value = _score_value(self.value)
        weight = _finite_number(self.weight, "score component weight")
        object.__setattr__(self, "value", value)
        object.__setattr__(self, "weight", weight)

    @property
    def contribution(self) -> float:
        if self.weight == 0:
            return 0.0
        if math.isinf(self.value):
            return math.inf
        contribution = self.value * self.weight
        if not math.isfinite(contribution):
            raise OptimizationError(
                f"Score component {self.name!r} contribution is not finite"
            )
        return contribution


@dataclass(frozen=True, slots=True)
class ScoreBreakdown:
    components: tuple[ScoreComponent, ...]

    def __post_init__(self) -> None:
        names = [item.name for item in self.components]
        if len(set(names)) != len(names):
            raise OptimizationError("Score component names must be unique")

    @property
    def total(self) -> float:
        contributions = [item.contribution for item in self.components]
        if any(math.isinf(value) for value in contributions):
            return math.inf
        try:
            total = math.fsum(contributions)
        except OverflowError as error:
            raise OptimizationError("Score total overflowed") from error
        if not math.isfinite(total):
            raise OptimizationError("Score total is not finite")
        return total


class PhysicalDesignScorer(ABC):
    @abstractmethod
    def score(
        self,
        graph: MaterialGraph,
        proposal: PhysicalDesignProposal,
    ) -> ScoreBreakdown:
        raise NotImplementedError


@dataclass(frozen=True, slots=True)
class WeightedPhysicalDesignScorer:
    scorer: PhysicalDesignScorer
    weight: float = 1.0

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "weight",
            _finite_number(self.weight, "physical scorer weight"),
        )


@dataclass(frozen=True, slots=True)
class CompositePhysicalDesignScorer(PhysicalDesignScorer):
    scorers: tuple[WeightedPhysicalDesignScorer, ...]

    def score(
        self,
        graph: MaterialGraph,
        proposal: PhysicalDesignProposal,
    ) -> ScoreBreakdown:
        components: list[ScoreComponent] = []
        for weighted in self.scorers:
            if weighted.weight == 0:
                continue
            breakdown = weighted.scorer.score(graph, proposal)
            components.extend(
                ScoreComponent(
                    component.name,
                    component.value,
                    component.weight * weighted.weight,
                )
                for component in breakdown.components
            )
        return ScoreBreakdown(tuple(components))


@dataclass(frozen=True, slots=True)
class OptimizationResult:
    proposal: PhysicalDesignProposal
    score: ScoreBreakdown


class DesignOptimizer(ABC):
    @abstractmethod
    def optimize(
        self,
        graph: MaterialGraph,
        candidates: Iterable[PhysicalDesignProposal],
        scorer: PhysicalDesignScorer,
    ) -> OptimizationResult:
        raise NotImplementedError


class BestScoreOptimizer(DesignOptimizer):
    def optimize(
        self,
        graph: MaterialGraph,
        candidates: Iterable[PhysicalDesignProposal],
        scorer: PhysicalDesignScorer,
    ) -> OptimizationResult:
        best: OptimizationResult | None = None
        saw_candidate = False
        for candidate in candidates:
            saw_candidate = True
            candidate.validate(graph)
            score = scorer.score(graph, candidate)
            if math.isinf(score.total):
                continue
            if best is None or score.total < best.score.total:
                best = OptimizationResult(candidate, score)
        if not saw_candidate:
            raise OptimizationError("Cannot optimize an empty candidate set")
        if best is None:
            raise OptimizationError("All physical design candidates are infeasible")
        return best


def _finite_number(value: object, context: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise OptimizationError(f"{context} must be numeric")
    result = float(value)
    if not math.isfinite(result):
        raise OptimizationError(f"{context} must be finite")
    return result


def _score_value(value: object) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise OptimizationError("Score component value must be numeric")
    result = float(value)
    if math.isnan(result) or result == -math.inf:
        raise OptimizationError(
            "Score component value must be finite or positive infinity"
        )
    return result
