from dataclasses import replace
import json
from pathlib import Path
import unittest
from unittest.mock import patch

from gateforge.behavior import BehaviorBuilder
from gateforge.compiler import (
    compilation_backend,
    compile_realization,
    materialize_search_candidates,
    realize_search_result,
    realize_material_candidates,
    start_compilation_search,
)
from gateforge.graph import MaterialGraph
from gateforge.hierarchy import PhysicalHierarchyMode, PhysicalHierarchyPolicy
from gateforge.material import MaterialDesignDigest, MaterialObjectId
from gateforge.placement import (
    PlacementError,
    ScoreBreakdown,
    ScoreComponent,
    TopologicalPlacementOptions,
    TopologicalPlacer,
)
from gateforge.providers.lbp.export import build_lbp_plan
from gateforge.realization import (
    DirectMaterialArtifact,
    DirectMaterialRealizationStrategy,
    RealizationError,
    RealizationInfeasibleError,
    RealizationOptions,
    RealizationOutcome,
    RealizationDesign,
    RealizationProvenance,
    RealizedEndpoint,
    RealizedEntity,
    RealizedObservation,
    RealizedEntityPlacement,
    RealizedPlacement,
)


FIXTURE = Path(__file__).parent / "fixtures" / "single_not.v"


class EmptyStrategy:
    def realize(self, material, providers, options):
        return ()


class UnavailableStrategy:
    def realize(self, material, providers, options):
        raise RealizationInfeasibleError("No legal placement")


class BrokenStrategy:
    def realize(self, material, providers, options):
        raise RuntimeError("Unexpected strategy defect")


class FixedStrategy:
    def __init__(self, outcomes):
        self.outcomes = outcomes

    def realize(self, material, providers, options):
        return self.outcomes


class DirectRealizationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.backend = compilation_backend()
        cls.search = start_compilation_search(str(FIXTURE)).finish()
        cls.materialized = materialize_search_candidates(
            cls.search, cls.backend.target_providers
        )
        cls.material = cls.materialized.candidates[0].material

    def test_direct_adapter_preserves_legacy_placement_and_export(self) -> None:
        options = RealizationOptions(
            placement=TopologicalPlacementOptions(column_pitch=120.0),
            physical_hierarchy=PhysicalHierarchyPolicy(
                mode=PhysicalHierarchyMode.PRESERVE_ALL
            ),
        )
        graph = MaterialGraph.from_design(self.material, self.backend.target_providers)
        expected = TopologicalPlacer(
            options.placement,
            providers=self.backend.target_providers,
            physical_hierarchy=options.physical_hierarchy,
            generated_hierarchy=options.generated_hierarchy,
        ).place(graph).finalize(graph)

        outcome = DirectMaterialRealizationStrategy().realize(
            self.material, self.backend.target_providers, options
        )[0]

        self.assertIsInstance(outcome.artifact, DirectMaterialArtifact)
        artifact = outcome.artifact
        assert isinstance(artifact, DirectMaterialArtifact)
        self.assertEqual(artifact.placement.canonical_data(), expected.canonical_data())
        self.assertEqual(
            build_lbp_plan(
                self.material, graph, artifact.placement, self.backend.target_providers
            ),
            build_lbp_plan(
                self.material, graph, expected, self.backend.target_providers
            ),
        )
        artifact.validate(self.material, self.backend.target_providers)
        self.assertEqual(
            outcome.score.total,
            self.materialized.candidates[0].score.provider_object_cost,
        )
        self.assertEqual(len(outcome.artifact_digest()), 64)
        self.assertEqual(outcome.artifact_digest(), outcome.artifact_digest())
        json.dumps(artifact.canonical_data(), allow_nan=False)

    def test_source_aware_strategy_receives_original_candidate_problem(self) -> None:
        received = []

        class SourceAwareStrategy:
            def realize_problem(self, problem, options):
                received.append(problem)
                return DirectMaterialRealizationStrategy().realize_problem(problem, options)

        result = realize_material_candidates(
            self.materialized, replace(self.backend, realization_strategy=SourceAwareStrategy())
        )
        self.assertIs(received[0].material, self.materialized.candidates[0].material)
        self.assertIs(received[0].behavior, self.materialized.candidates[0].behavior)
        self.assertIs(result.winner.baseline, self.materialized.candidates[0])

    def inventory(self):
        builder = BehaviorBuilder()
        value = builder.input("a", 32)
        behavior = builder.build({"y": value})
        design = RealizationDesign(
            "test", self.material.get_digest(), behavior.get_digest(),
            (RealizedEntity("generated", "terminal", ("out", "in")),),
            (RealizedObservation("y", value, (RealizedEndpoint("generated", "out"),)),),
            (
                RealizationProvenance("identity/v1", ("generated",), (), (value,)),
                RealizationProvenance(
                    "removed/v1", (), (self.material.objects[0].identifier,), (value,)
                ),
            ),
        )
        return design, behavior

    def test_inventory_owns_generated_entities_and_removed_object_provenance(self) -> None:
        design, behavior = self.inventory()
        design.validate(self.material, behavior)
        reordered = replace(
            design,
            entities=(replace(design.entities[0], ports=("in", "out")),),
            provenance=tuple(reversed(design.provenance)),
        )
        self.assertEqual(design.get_digest(), reordered.get_digest())
        self.assertNotEqual(
            design.get_digest(), replace(design, entities=design.entities + (
                RealizedEntity("relay", "relay", ("circuit",)),
            )).get_digest(),
        )
        self.assertNotIn(self.material.objects[0].identifier.value, {
            item.identifier for item in design.entities
        })

    def test_inventory_rejects_stale_provenance_and_missing_endpoints(self) -> None:
        design, behavior = self.inventory()
        for invalid, message in (
            (replace(design, material_digest=MaterialDesignDigest("0" * 64)), "material digest"),
            (replace(design, behavior_digest="0" * 64), "behavior digest"),
            (replace(design, observations=()), "observations"),
            (replace(design, provenance=(RealizationProvenance(
                "bad/v1", material_objects=(MaterialObjectId("missing"),)
            ),)), "material object"),
            (replace(design, provenance=(RealizationProvenance(
                "bad/v1", values=("missing",)
            ),)), "behavior value"),
        ):
            with self.subTest(message=message), self.assertRaisesRegex(RealizationError, message):
                invalid.validate(self.material, behavior)
        with self.assertRaisesRegex(RealizationError, "unknown realized endpoint"):
            replace(design, entities=())
        with self.assertRaisesRegex(RealizationError, "unknown realized entity"):
            replace(design, provenance=(RealizationProvenance("bad/v1", ("missing",)),))

    def test_realized_placement_has_independent_strict_inventory_binding(self) -> None:
        design, _ = self.inventory()
        placement = RealizedPlacement(design.get_digest(), (RealizedEntityPlacement("generated", 0, 1),))
        placement.validate(design)
        self.assertEqual(placement.canonical_data()["schema_version"], 1)
        with self.assertRaisesRegex(RealizationError, "inventory digest"):
            replace(placement, realization_digest="0" * 64).validate(design)
        with self.assertRaisesRegex(RealizationError, "exactly its inventory"):
            replace(placement, entities=()).validate(design)
        with self.assertRaisesRegex(RealizationError, "Duplicate"):
            replace(placement, entities=placement.entities * 2)
        for value in (True, float("nan"), float("inf")):
            with self.subTest(value=value), self.assertRaisesRegex(RealizationError, "finite"):
                RealizedEntityPlacement("generated", value, 0)

    def test_direct_artifact_rejects_mismatched_material_digest(self) -> None:
        outcome = DirectMaterialRealizationStrategy().realize(
            self.material, self.backend.target_providers, RealizationOptions()
        )[0]
        artifact = outcome.artifact
        assert isinstance(artifact, DirectMaterialArtifact)
        invalid = DirectMaterialArtifact(replace(
            artifact.placement, material_digest=MaterialDesignDigest("0" * 64)
        ))
        with self.assertRaisesRegex(PlacementError, "material digest"):
            invalid.validate(self.material, self.backend.target_providers)

    def test_backend_defaults_and_source_entry_point(self) -> None:
        backend = replace(
            self.backend,
            placement_options=TopologicalPlacementOptions(column_pitch=150.0),
        )
        result = compile_realization(str(FIXTURE), backend=backend)
        artifact = result.winner.outcome.artifact
        assert isinstance(artifact, DirectMaterialArtifact)
        self.assertEqual(
            artifact.placement.provenance.options.canonical_data()["column_pitch"],
            150.0,
        )
        self.assertEqual(len(result.candidates), 1)
        self.assertIsNone(result.materialized.report.winner)

    def test_factorio_compiles_through_registered_source_aware_finalizer(self) -> None:
        backend = compilation_backend("factorio")
        result = compile_realization(str(FIXTURE.parent / "factorio" / "add32.v"), backend=backend)
        self.assertEqual(result.winner.outcome.artifact.target, "factorio")
        self.assertEqual(backend.realization_presenter.details(result.winner.outcome.artifact)["combinators"], 1)
        with self.assertRaisesRegex(RealizationError, "no realization strategy"):
            compile_realization("missing.v", backend=replace(backend, realization_strategy=None))

    def test_foreign_artifacts_fail_instead_of_becoming_candidates(self) -> None:
        outcome = DirectMaterialRealizationStrategy().realize(
            self.material, self.backend.target_providers, RealizationOptions()
        )[0]
        artifact = outcome.artifact
        assert isinstance(artifact, DirectMaterialArtifact)
        foreign = RealizationOutcome(
            DirectMaterialArtifact(replace(artifact.placement, target="factorio")),
            outcome.score,
        )
        with self.assertRaisesRegex(RealizationError, "different backend"):
            realize_search_result(
                self.search,
                replace(self.backend, realization_strategy=FixedStrategy((foreign,))),
            )

    def test_multiple_outcomes_are_ranked_independently_of_emission_order(self) -> None:
        outcome = DirectMaterialRealizationStrategy().realize(
            self.material, self.backend.target_providers, RealizationOptions()
        )[0]
        outcomes = tuple(
            replace(outcome, score=ScoreBreakdown((ScoreComponent("cost", cost),)))
            for cost in (3.0, 1.0)
        )
        for order in (outcomes, tuple(reversed(outcomes))):
            result = realize_search_result(
                self.search,
                replace(self.backend, realization_strategy=FixedStrategy(order)),
            )
            self.assertEqual(
                [candidate.outcome.score.total for candidate in result.candidates],
                [1.0, 3.0],
            )
            self.assertEqual(len(result.materialized.candidates), 1)

    def test_infeasible_and_empty_strategies_fail_explicitly(self) -> None:
        for strategy, message in (
            (EmptyStrategy(), "produced no realizations"),
            (UnavailableStrategy(), "No legal placement"),
        ):
            with self.subTest(strategy=type(strategy).__name__):
                with self.assertRaisesRegex(RealizationInfeasibleError, message):
                    realize_search_result(
                        self.search, replace(self.backend, realization_strategy=strategy)
                    )

    def test_strategy_defects_are_not_silently_pruned(self) -> None:
        with self.assertRaisesRegex(RuntimeError, "Unexpected strategy defect"):
            realize_search_result(
                self.search,
                replace(self.backend, realization_strategy=BrokenStrategy()),
            )

    def test_placement_failure_is_classified_as_infeasible(self) -> None:
        with patch.object(
            TopologicalPlacer, "place", side_effect=PlacementError("No legal placement")
        ):
            with self.assertRaisesRegex(RealizationInfeasibleError, "No legal placement"):
                DirectMaterialRealizationStrategy().realize(
                    self.material, self.backend.target_providers, RealizationOptions()
                )

    def test_realization_costs_reject_negative_or_infinite_values(self) -> None:
        artifact = DirectMaterialRealizationStrategy().realize(
            self.material, self.backend.target_providers, RealizationOptions()
        )[0].artifact
        for component in (
            ScoreComponent("cost", -1),
            ScoreComponent("cost", 1, -1),
            ScoreComponent("cost", float("inf")),
        ):
            with self.subTest(component=component):
                with self.assertRaises(RealizationError):
                    RealizationOutcome(artifact, ScoreBreakdown((component,)))


if __name__ == "__main__":
    unittest.main()