from dataclasses import dataclass
import math
import unittest

from gateforge.graph import MaterialGraph
from gateforge.material import MaterialDesignDigest
from gateforge.placement import (
    BestScoreOptimizer,
    CompositePhysicalDesignScorer,
    OptimizationError,
    PhysicalDesignProposal,
    PhysicalDesignScorer,
    PlacementPhysicalDesignProposal,
    ScoreBreakdown,
    ScoreComponent,
    TopologicalPlacer,
    WeightedPhysicalDesignScorer,
)
from gateforge.providers.lbp.common import LBP_PROVIDER
from gateforge.providers.lbp.objects import make_lbp_provider
from tests.test_graph import _lbp_inverter_design


@dataclass(frozen=True, slots=True)
class TaggedProposal(PhysicalDesignProposal):
    placement: PlacementPhysicalDesignProposal
    tag: str

    def material_digest(self) -> MaterialDesignDigest:
        return self.placement.material_digest()

    def resolve_placement(self, graph: MaterialGraph):
        return self.placement.resolve_placement(graph)


class TagScorer(PhysicalDesignScorer):
    def __init__(self, name: str, values: dict[str, float]) -> None:
        self.name = name
        self.values = values
        self.calls = 0

    def score(
        self,
        graph: MaterialGraph,
        proposal: PhysicalDesignProposal,
    ) -> ScoreBreakdown:
        self.calls += 1
        if not isinstance(proposal, TaggedProposal):
            raise TypeError("TagScorer requires TaggedProposal")
        return ScoreBreakdown((ScoreComponent(self.name, self.values[proposal.tag]),))


class FailingScorer(PhysicalDesignScorer):
    def score(self, graph, proposal):
        raise AssertionError("Disabled scorer must not be evaluated")


class PlacementOptimizationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.graph = MaterialGraph.from_design(
            _lbp_inverter_design(),
            {LBP_PROVIDER: make_lbp_provider()},
        )
        placement = PlacementPhysicalDesignProposal(
            TopologicalPlacer().place(self.graph)
        )
        self.left = TaggedProposal(placement, "left")
        self.right = TaggedProposal(placement, "right")

    def test_placement_physical_proposal_resolves_placement(self) -> None:
        placed = self.left.resolve_placement(self.graph)

        self.assertEqual(placed.material_digest, self.graph.design.get_digest())

    def test_composite_flattens_weights_and_supports_rewards(self) -> None:
        scorer = CompositePhysicalDesignScorer(
            (
                WeightedPhysicalDesignScorer(
                    TagScorer("cost", {"left": 5.0}),
                    2.0,
                ),
                WeightedPhysicalDesignScorer(
                    TagScorer("reward", {"left": 3.0}),
                    -1.0,
                ),
            )
        )

        score = scorer.score(self.graph, self.left)

        self.assertEqual(score.total, 7.0)
        self.assertEqual(
            [(item.name, item.weight) for item in score.components],
            [("cost", 2.0), ("reward", -1.0)],
        )

    def test_zero_weight_disables_child_without_evaluating_it(self) -> None:
        scorer = CompositePhysicalDesignScorer(
            (WeightedPhysicalDesignScorer(FailingScorer(), 0.0),)
        )

        self.assertEqual(scorer.score(self.graph, self.left).total, 0.0)

    def test_infinity_dominates_even_with_negative_weight(self) -> None:
        scorer = CompositePhysicalDesignScorer(
            (
                WeightedPhysicalDesignScorer(
                    TagScorer("invalid", {"left": math.inf}),
                    -1.0,
                ),
            )
        )

        self.assertEqual(scorer.score(self.graph, self.left).total, math.inf)

    def test_duplicate_component_names_are_rejected(self) -> None:
        scorer = CompositePhysicalDesignScorer(
            (
                WeightedPhysicalDesignScorer(
                    TagScorer("cost", {"left": 1.0}),
                ),
                WeightedPhysicalDesignScorer(
                    TagScorer("cost", {"left": 2.0}),
                ),
            )
        )

        with self.assertRaises(OptimizationError):
            scorer.score(self.graph, self.left)

    def test_optimizer_selects_minimum_and_preserves_first_tie(self) -> None:
        scorer = TagScorer("cost", {"left": 1.0, "right": 1.0})

        result = BestScoreOptimizer().optimize(
            self.graph,
            (self.left, self.right),
            scorer,
        )

        self.assertIs(result.proposal, self.left)
        self.assertEqual(result.score.total, 1.0)

    def test_optimizer_rejects_empty_and_all_infeasible_candidates(self) -> None:
        optimizer = BestScoreOptimizer()
        with self.assertRaises(OptimizationError):
            optimizer.optimize(
                self.graph,
                (),
                TagScorer("cost", {}),
            )
        with self.assertRaises(OptimizationError):
            optimizer.optimize(
                self.graph,
                (self.left, self.right),
                TagScorer("invalid", {"left": math.inf, "right": math.inf}),
            )


if __name__ == "__main__":
    unittest.main()
