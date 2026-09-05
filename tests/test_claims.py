from dataclasses import replace
from pathlib import Path
import unittest

from pyosys import libyosys as ys

from gateforge.claims import ClaimApplicationError, accept_mapping_proposals
from gateforge.design import DesignContext, rtlil_id
from gateforge.mapping import Mapper
from gateforge.providers.lbp.common import LBP_PROVIDER
from gateforge.providers.lbp.mappers import LBPCombinatorialLowLevelGateMapper
from gateforge.providers.lbp.objects import make_lbp_provider
from gateforge.state import CompilationIntermediateState


FIXTURE = Path(__file__).parents[1] / "scratch" / "basic.v"


def _lowered_context() -> DesignContext:
    context = DesignContext(ys.Design())
    context.run_pass(f"read_verilog {FIXTURE}")
    context.run_pass("hierarchy -check -auto-top")
    context.run_pass("proc")
    context.run_pass("fsm -nomap")
    context.run_pass("opt -full")
    context.run_pass("techmap")
    return context


class ClaimApplicationTests(unittest.TestCase):
    def test_acceptance_replaces_cells_with_semantic_prefab_claims(self) -> None:
        context = _lowered_context()
        state = CompilationIntermediateState.empty(context.revision)
        proposals = Mapper([LBPCombinatorialLowLevelGateMapper()]).map_design(
            context.snapshot()
        )
        source_cells = {
            (cell.module, cell.name)
            for proposal in proposals
            for cell in proposal.ids
        }

        state = accept_mapping_proposals(
            context,
            state,
            proposals,
            {LBP_PROVIDER: make_lbp_provider()},
        )

        self.assertEqual(len(state.claims), 8)
        self.assertEqual(len(state.prefabs), 3)
        self.assertEqual(state.revision, context.revision)
        for module_name, cell_name in source_cells:
            module = context.design.module(rtlil_id(module_name))
            self.assertIsNone(module.cell(rtlil_id(cell_name)))
        for claim in state.claims.values():
            module = context.design.module(rtlil_id(claim.module))
            cell = module.cell(rtlil_id(claim.instance))
            self.assertIsNotNone(cell)
            self.assertEqual(
                cell.get_string_attribute(rtlil_id("gateforge_prefab")),
                claim.prefab.value,
            )
            self.assertFalse(any(hasattr(port, "source") for port in claim.ports))

    def test_invalid_boundary_is_rejected_before_mutation(self) -> None:
        context = _lowered_context()
        state = CompilationIntermediateState.empty(context.revision)
        proposal = Mapper([LBPCombinatorialLowLevelGateMapper()]).map_design(
            context.snapshot()
        )[0]
        invalid = replace(
            proposal,
            boundary=frozenset(tuple(proposal.boundary)[1:]),
        )
        module_name = next(iter(proposal.ids)).module
        module = context.design.module(rtlil_id(module_name))
        cell_count = module.cells_size()
        revision = context.revision

        with self.assertRaises(ClaimApplicationError):
            accept_mapping_proposals(
                context,
                state,
                [invalid],
                {LBP_PROVIDER: make_lbp_provider()},
            )

        self.assertEqual(module.cells_size(), cell_count)
        self.assertEqual(context.revision, revision)
        for identifier in proposal.ids:
            self.assertIsNotNone(module.cell(rtlil_id(identifier.name)))

    def test_claims_survive_abc_without_source_provenance_lookup(self) -> None:
        context = _lowered_context()
        state = CompilationIntermediateState.empty(context.revision)
        proposals = Mapper([LBPCombinatorialLowLevelGateMapper()]).map_design(
            context.snapshot()
        )
        state = accept_mapping_proposals(
            context,
            state,
            proposals,
            {LBP_PROVIDER: make_lbp_provider()},
        )

        context.run_pass("abc")
        state = state.with_revision(context.revision)

        for claim in state.claims.values():
            module = context.design.module(rtlil_id(claim.module))
            self.assertIsNotNone(module.cell(rtlil_id(claim.instance)))


if __name__ == "__main__":
    unittest.main()