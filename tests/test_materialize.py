from pathlib import Path
import unittest

from gateforge.gateforge import compile_physical, compile_source
from gateforge.materialize import flatten_and_materialize
from gateforge.providers.lbp.common import LBP_PROVIDER
from gateforge.providers.lbp.objects import make_lbp_provider


FIXTURE = Path(__file__).parent / "fixtures" / "hierarchy.v"
RENAMED_FIXTURE = Path(__file__).parent / "fixtures" / "hierarchy_renamed.v"
PARAMETERIZED_FIXTURE = (
    Path(__file__).parent / "fixtures" / "parameterized_hierarchy.v"
)


class MaterializationTests(unittest.TestCase):
    def test_flatten_expands_one_template_claim_into_two_physical_objects(self) -> None:
        context, state = compile_source(str(FIXTURE))

        self.assertEqual(len(state.claims), 1)
        self.assertEqual(len(state.prefabs), 1)

        state, physical = flatten_and_materialize(
            context,
            state,
            {LBP_PROVIDER: make_lbp_provider()},
        )

        self.assertEqual(state.revision, context.revision)
        self.assertEqual(len(physical.objects), 2)
        self.assertEqual(len({item.identifier for item in physical.objects}), 2)
        self.assertEqual(len({item.prefab for item in physical.objects}), 1)
        self.assertEqual(len({item.occurrence for item in physical.objects}), 2)
        self.assertEqual(len(physical.nets), 4)

    def test_anchored_occurrence_ids_survive_hdl_instance_renames(self) -> None:
        _, _, original = compile_physical(str(FIXTURE))
        _, _, renamed = compile_physical(str(RENAMED_FIXTURE))

        self.assertEqual(
            {item.identifier for item in original.objects},
            {item.identifier for item in renamed.objects},
        )

    def test_physical_compilation_is_reproducible(self) -> None:
        _, _, first = compile_physical(str(FIXTURE))
        _, _, second = compile_physical(str(FIXTURE))

        self.assertEqual(first.canonical_data(), second.canonical_data())

    def test_parameterized_module_specializations_materialize(self) -> None:
        _, state, physical = compile_physical(str(PARAMETERIZED_FIXTURE))

        self.assertEqual(len(state.claims), 3)
        self.assertEqual(len(state.prefabs), 1)
        self.assertEqual(len(physical.objects), 3)
        self.assertEqual(
            {item.hierarchy for item in physical.objects},
            {"module:top/anchor:narrow", "module:top/anchor:wide"},
        )


if __name__ == "__main__":
    unittest.main()