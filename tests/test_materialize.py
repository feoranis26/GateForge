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
NESTED_FIXTURE = Path(__file__).parents[1] / "scratch" / "basic_nested.v"


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

    def test_materialization_preserves_module_occurrences_and_boundary_nets(self) -> None:
        _, _, material = compile_material(str(NESTED_FIXTURE))

        modules = {item.path: item for item in material.modules}
        root = modules["module:test_with_inverters"]
        left = modules["module:test_with_inverters/name:inv1"]
        right = modules["module:test_with_inverters/name:inv2"]

        self.assertEqual(root.parent, None)
        self.assertEqual(root.children, (left.path, right.path))
        self.assertEqual(left.module, "myinverter")
        self.assertEqual(right.module, "myinverter")
        self.assertEqual(len(root.objects), 5)
        self.assertEqual(len(left.objects), 1)
        self.assertEqual(len(right.objects), 1)
        root_ports = {(item.name, item.bit): item.net for item in root.ports}
        left_ports = {(item.name, item.bit): item.net for item in left.ports}
        right_ports = {(item.name, item.bit): item.net for item in right.ports}
        self.assertEqual(left_ports[("i", 0)], root_ports[("myinput", 0)])
        self.assertEqual(right_ports[("i", 0)], root_ports[("myotherinput", 0)])
        self.assertIsNotNone(left_ports[("o", 0)])
        self.assertIsNotNone(right_ports[("o", 0)])

        implementation_objects = {
            identifier
            for implementation in material.implementations
            for identifier in implementation.objects
        }
        self.assertEqual(
            implementation_objects,
            {item.identifier for item in material.objects},
        )
        self.assertTrue(
            all(
                implementation.owner_module in modules
                for implementation in material.implementations
            )
        )

    def test_material_hierarchy_round_trips(self) -> None:
        _, _, material = compile_material(str(NESTED_FIXTURE))
        restored = type(material).from_canonical_data(
            material.canonical_data(),
            {LBP_PROVIDER: make_lbp_provider()},
        )

        self.assertEqual(restored, material)


if __name__ == "__main__":
    unittest.main()