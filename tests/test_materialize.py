from pathlib import Path
import unittest

from gateforge.gateforge import compile_material, compile_source
from gateforge.materialize import flatten_and_materialize
from gateforge.providers.lbp.common import LBP_PROVIDER
from gateforge.providers.lbp.objects import make_lbp_provider


FIXTURE = Path(__file__).parent / "fixtures" / "hierarchy.v"
RENAMED_FIXTURE = Path(__file__).parent / "fixtures" / "hierarchy_renamed.v"
PARAMETERIZED_FIXTURE = (
    Path(__file__).parent / "fixtures" / "parameterized_hierarchy.v"
)


class MaterializationTests(unittest.TestCase):
    def test_flatten_expands_one_template_claim_into_two_material_objects(self) -> None:
        context, state = compile_source(str(FIXTURE))

        self.assertEqual(len(state.claims), 1)
        self.assertEqual(len(state.prefabs), 1)

        state, material = flatten_and_materialize(
            context,
            state,
            {LBP_PROVIDER: make_lbp_provider()},
        )

        self.assertEqual(state.revision, context.revision)
        self.assertEqual(len(material.objects), 2)
        self.assertEqual(len({item.identifier for item in material.objects}), 2)
        self.assertEqual(len({item.prefab for item in material.objects}), 1)
        self.assertEqual(len({item.occurrence for item in material.objects}), 2)
        self.assertEqual(len(material.nets), 4)

    def test_anchored_occurrence_ids_survive_hdl_instance_renames(self) -> None:
        _, _, original = compile_material(str(FIXTURE))
        _, _, renamed = compile_material(str(RENAMED_FIXTURE))

        self.assertEqual(
            {item.identifier for item in original.objects},
            {item.identifier for item in renamed.objects},
        )

    def test_material_compilation_is_reproducible(self) -> None:
        _, _, first = compile_material(str(FIXTURE))
        _, _, second = compile_material(str(FIXTURE))

        self.assertEqual(first.canonical_data(), second.canonical_data())

    def test_parameterized_module_specializations_materialize(self) -> None:
        _, state, material = compile_material(str(PARAMETERIZED_FIXTURE))

        self.assertEqual(len(state.claims), 3)
        self.assertEqual(len(state.prefabs), 1)
        self.assertEqual(len(material.objects), 3)
        self.assertEqual(
            {item.hierarchy for item in material.objects},
            {"module:top/anchor:narrow", "module:top/anchor:wide"},
        )


if __name__ == "__main__":
    unittest.main()