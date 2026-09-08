from pathlib import Path
import unittest

from gateforge.design import DesignContext
from gateforge.gateforge import design_preprocess
from gateforge.mapping import (
    BoundaryBinding,
    Mapper,
    MappingCostEstimate,
    MappingProposal,
    MappingProvider,
)
from gateforge.pipeline import MappingStage
from gateforge.providers.lbp.common import LBP_LOGIC, LBP_PROVIDER, LBP_WIRE
from gateforge.providers.lbp.mappers import LBPCombinatorialLowLevelGateMapper
from gateforge.providers.lbp.objects import make_lbp_provider
from gateforge.providers.lbp.types import LBPNotGateType
from gateforge.search import (
    CompilationSearch,
    MappingSearchMode,
    MappingSearchOptions,
    SearchCandidateStatus,
)
from gateforge.source import DesignSnapshot, SnapshotBitRef
from gateforge.state import CompilationIntermediateState
from gateforge.target import (
    ObjectPortRef,
    PortDirection,
    PrefabNet,
    PrefabObject,
    PrefabPort,
    PrefabPortRef,
    SemanticPrefab,
)


FIXTURE = Path(__file__).parent / "fixtures" / "single_not.v"


def _not_prefab() -> SemanticPrefab:
    return SemanticPrefab(
        provider=LBP_PROVIDER,
        objects=frozenset(
            {PrefabObject("gate", LBPNotGateType(width=1, invert=False).get_type())}
        ),
        ports=frozenset(
            {
                PrefabPort("A", PortDirection.INPUT, LBP_LOGIC),
                PrefabPort("Y", PortDirection.OUTPUT, LBP_LOGIC),
            }
        ),
        nets=frozenset(
            {
                PrefabNet(
                    "a",
                    LBP_WIRE,
                    frozenset(
                        {PrefabPortRef("A"), ObjectPortRef("gate", "IN_0")}
                    ),
                ),
                PrefabNet(
                    "y",
                    LBP_WIRE,
                    frozenset(
                        {ObjectPortRef("gate", "OUT"), PrefabPortRef("Y")}
                    ),
                ),
            }
        ),
    )


def _triple_not_prefab() -> SemanticPrefab:
    not_type = LBPNotGateType(width=1, invert=False).get_type()
    return SemanticPrefab(
        provider=LBP_PROVIDER,
        objects=frozenset(
            PrefabObject(role, not_type) for role in ("first", "second", "third")
        ),
        ports=frozenset(
            {
                PrefabPort("A", PortDirection.INPUT, LBP_LOGIC),
                PrefabPort("Y", PortDirection.OUTPUT, LBP_LOGIC),
            }
        ),
        nets=frozenset(
            {
                PrefabNet(
                    "a",
                    LBP_WIRE,
                    frozenset(
                        {PrefabPortRef("A"), ObjectPortRef("first", "IN_0")}
                    ),
                ),
                PrefabNet(
                    "first",
                    LBP_WIRE,
                    frozenset(
                        {
                            ObjectPortRef("first", "OUT"),
                            ObjectPortRef("second", "IN_0"),
                        }
                    ),
                ),
                PrefabNet(
                    "second",
                    LBP_WIRE,
                    frozenset(
                        {
                            ObjectPortRef("second", "OUT"),
                            ObjectPortRef("third", "IN_0"),
                        }
                    ),
                ),
                PrefabNet(
                    "y",
                    LBP_WIRE,
                    frozenset(
                        {ObjectPortRef("third", "OUT"), PrefabPortRef("Y")}
                    ),
                ),
            }
        ),
    )


class SourceNotMapper(MappingProvider):
    provider = LBP_PROVIDER

    def map(self, design: DesignSnapshot) -> tuple[MappingProposal, ...]:
        proposals: list[MappingProposal] = []
        for module in design.modules.values():
            for cell in module.cells.values():
                if cell.identifier.expected_type != "$logic_not":
                    continue
                input_bit = cell.ports["A"].bits[0]
                output_bit = cell.ports["Y"].bits[0]
                if not isinstance(input_bit, SnapshotBitRef) or not isinstance(
                    output_bit, SnapshotBitRef
                ):
                    raise AssertionError("Test NOT cell unexpectedly uses a constant")
                proposals.append(
                    MappingProposal(
                        revision=design.revision,
                        provider=self.provider,
                        mapper="test.source_not",
                        rule="$logic_not",
                        rule_version=1,
                        ids=frozenset({cell.identifier}),
                        prefab=_not_prefab(),
                        boundary=frozenset(
                            {
                                BoundaryBinding(
                                    input_bit,
                                    PrefabPortRef("A"),
                                ),
                                BoundaryBinding(
                                    output_bit,
                                    PrefabPortRef("Y"),
                                ),
                            }
                        ),
                        cost=MappingCostEstimate(5.0, 5.0),
                    )
                )
        return tuple(proposals)


class MisleadingExpensiveSourceNotMapper(SourceNotMapper):
    def map(self, design: DesignSnapshot) -> tuple[MappingProposal, ...]:
        return tuple(
            MappingProposal(
                revision=proposal.revision,
                provider=proposal.provider,
                mapper="test.expensive_source_not",
                rule=proposal.rule,
                rule_version=proposal.rule_version,
                ids=proposal.ids,
                prefab=_triple_not_prefab(),
                boundary=proposal.boundary,
                cost=MappingCostEstimate(0.0, 0.0),
            )
            for proposal in super().map(design)
        )


def _search_context() -> tuple[DesignContext, CompilationIntermediateState]:
    context = design_preprocess(str(FIXTURE))
    return context, CompilationIntermediateState.empty(context.revision)


class CompilationSearchTests(unittest.TestCase):
    def test_stage_session_matches_all_at_once_search_in_every_mode(self) -> None:
        stages = (
            MappingStage("source"),
            MappingStage("leaf", ("techmap",)),
        )
        for options in (
            MappingSearchOptions(mode=MappingSearchMode.GREEDY),
            MappingSearchOptions(mode=MappingSearchMode.BEAM, beam_width=1),
            MappingSearchOptions(mode=MappingSearchMode.EXHAUSTIVE),
        ):
            with self.subTest(mode=options.mode.value):
                providers = {LBP_PROVIDER: make_lbp_provider()}
                mapper = Mapper(
                    [SourceNotMapper(), LBPCombinatorialLowLevelGateMapper()]
                )
                initial_context, initial_state = _search_context()
                initial_checkpoint = initial_context.checkpoint()
                direct = CompilationSearch(mapper, providers, options).run(
                    initial_checkpoint.restore(),
                    initial_state,
                    stages,
                )
                session = CompilationSearch(mapper, providers, options).start(
                    initial_checkpoint.restore(),
                    initial_state,
                    stages,
                )

                source_history = session.advance_stage()
                leaf_history = session.advance_stage()
                stepped = session.result()

                def candidate_behavior(candidate):
                    return (
                        candidate.stage_index,
                        tuple(
                            sorted(
                                claim.mapper
                                for claim in candidate.state.claims.values()
                            )
                        ),
                        len(candidate.state.claims),
                        candidate.lower_bound,
                        candidate.expected_cost,
                    )

                self.assertEqual(
                    tuple(candidate_behavior(item) for item in stepped.candidates),
                    tuple(candidate_behavior(item) for item in direct.candidates),
                )
                self.assertEqual(stepped.report, direct.report)
                self.assertTrue(session.complete)
                self.assertEqual(session.reports, direct.report.stages)
                self.assertEqual(len(session.history), 2)
                self.assertEqual(source_history.stage, "source")
                self.assertTrue(source_history.expansions[0].proposals)
                self.assertEqual(
                    {transition.status for transition in leaf_history.transitions},
                    {SearchCandidateStatus.FRONTIER},
                )

    def test_exhaustive_search_preserves_direct_and_lowered_branches(self) -> None:
        context, state = _search_context()
        search = CompilationSearch(
            Mapper([SourceNotMapper(), LBPCombinatorialLowLevelGateMapper()]),
            {LBP_PROVIDER: make_lbp_provider()},
            MappingSearchOptions(mode=MappingSearchMode.EXHAUSTIVE),
        )

        result = search.run(
            context,
            state,
            (
                MappingStage("source"),
                MappingStage("leaf", ("techmap",)),
            ),
        )

        self.assertEqual(len(result.candidates), 2)
        self.assertEqual(
            {
                next(iter(candidate.state.claims.values())).mapper
                for candidate in result.candidates
            },
            {"test.source_not", "lbp.combinatorial.low_level"},
        )
        self.assertEqual(result.report.stages[0].output_candidates, 2)

    def test_beam_search_prefers_lower_expected_cost(self) -> None:
        context, state = _search_context()
        search = CompilationSearch(
            Mapper([SourceNotMapper(), LBPCombinatorialLowLevelGateMapper()]),
            {LBP_PROVIDER: make_lbp_provider()},
            MappingSearchOptions(mode=MappingSearchMode.BEAM, beam_width=1),
        )

        result = search.run(
            context,
            state,
            (
                MappingStage("source"),
                MappingStage("leaf", ("techmap",)),
            ),
        )

        self.assertEqual(len(result.candidates), 1)
        claim = next(iter(result.candidates[0].state.claims.values()))
        self.assertEqual(claim.mapper, "lbp.combinatorial.low_level")
        self.assertEqual(result.candidates[0].expected_cost, 1.0)

    def test_stage_history_marks_beam_frontier_and_pruned_branches(self) -> None:
        context, state = _search_context()
        session = CompilationSearch(
            Mapper([SourceNotMapper(), LBPCombinatorialLowLevelGateMapper()]),
            {LBP_PROVIDER: make_lbp_provider()},
            MappingSearchOptions(mode=MappingSearchMode.BEAM, beam_width=1),
        ).start(
            context,
            state,
            (
                MappingStage("source"),
                MappingStage("leaf", ("techmap",)),
            ),
        )

        history = session.advance_stage()

        self.assertEqual(
            {transition.status for transition in history.transitions},
            {SearchCandidateStatus.FRONTIER, SearchCandidateStatus.PRUNED},
        )
        self.assertEqual(history.report.pruned_candidates, 1)

    def test_terminal_material_cost_overrides_misleading_proposal_estimate(self) -> None:
        from gateforge.gateforge import compile_material_with_report

        _, state, material, report = compile_material_with_report(
            str(FIXTURE),
            stages=(
                MappingStage("source"),
                MappingStage("leaf", ("techmap",)),
            ),
            mapping_providers=(
                MisleadingExpensiveSourceNotMapper(),
                LBPCombinatorialLowLevelGateMapper(),
            ),
            mapping_search=MappingSearchOptions(mode=MappingSearchMode.EXHAUSTIVE),
        )

        self.assertEqual(len(material.objects), 1)
        self.assertEqual(
            next(iter(state.claims.values())).mapper,
            "lbp.combinatorial.low_level",
        )
        self.assertEqual(len(report.terminal_scores), 2)
        self.assertIsNotNone(report.winner)
        self.assertEqual(
            report.winner.value,
            next(
                item.candidate.value
                for item in report.terminal_scores
                if item.object_count == 1
            ),
        )
        self.assertEqual(report.canonical_data()["schema_version"], 1)


if __name__ == "__main__":
    unittest.main()