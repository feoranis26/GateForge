from pathlib import Path
import unittest

from gateforge.gateforge import compile_material
from gateforge.hierarchy import (
    PhysicalHierarchyMode,
    PhysicalHierarchyPolicy,
    SynthesisHierarchyMode,
    SynthesisHierarchyPolicy,
    retained_module_paths,
)
from gateforge.providers.lbp.common import LBP_PROVIDER
from gateforge.providers.lbp.objects import make_lbp_provider


FIXTURE = Path(__file__).parents[1] / "scratch" / "basic_nested.v"
PROVIDERS = {LBP_PROVIDER: make_lbp_provider()}


class HierarchyPolicyTests(unittest.TestCase):
    def test_synthesis_preserve_keeps_module_occurrences(self) -> None:
        _, _, material = compile_material(
            str(FIXTURE),
            synthesis_hierarchy=SynthesisHierarchyPolicy(
                SynthesisHierarchyMode.PRESERVE
            ),
        )

        self.assertEqual(len(material.modules), 3)
        self.assertEqual(len(material.objects), 7)

    def test_synthesis_flat_allows_cross_module_optimization(self) -> None:
        _, _, material = compile_material(
            str(FIXTURE),
            synthesis_hierarchy=SynthesisHierarchyPolicy(
                SynthesisHierarchyMode.FLAT
            ),
        )

        self.assertEqual(len(material.modules), 1)
        self.assertLess(len(material.objects), 7)

    def test_synthesis_min_cells_flattens_only_below_threshold(self) -> None:
        _, _, preserved = compile_material(
            str(FIXTURE),
            synthesis_hierarchy=SynthesisHierarchyPolicy(
                SynthesisHierarchyMode.MIN_CELLS,
                1,
            ),
        )
        _, _, flattened = compile_material(
            str(FIXTURE),
            synthesis_hierarchy=SynthesisHierarchyPolicy(
                SynthesisHierarchyMode.MIN_CELLS,
                2,
            ),
        )

        self.assertEqual(len(preserved.modules), 3)
        self.assertEqual(len(flattened.modules), 1)

    def test_physical_flat_retains_only_root(self) -> None:
        _, _, material = compile_material(str(FIXTURE))

        retained = retained_module_paths(
            material,
            PhysicalHierarchyPolicy(PhysicalHierarchyMode.FLAT),
            PROVIDERS,
        )

        self.assertEqual(retained, frozenset({"module:test_with_inverters"}))

    def test_physical_preserve_all_keeps_empty_wrappers(self) -> None:
        _, _, material = compile_material(str(FIXTURE))

        retained = retained_module_paths(
            material,
            PhysicalHierarchyPolicy(PhysicalHierarchyMode.PRESERVE_ALL),
            PROVIDERS,
        )

        self.assertEqual(retained, frozenset(item.path for item in material.modules))

    def test_physical_min_objects_retains_only_large_occurrences(self) -> None:
        _, _, material = compile_material(str(FIXTURE))

        retained = retained_module_paths(
            material,
            PhysicalHierarchyPolicy(PhysicalHierarchyMode.MIN_OBJECTS, 2),
            PROVIDERS,
        )

        self.assertEqual(retained, frozenset({"module:test_with_inverters"}))

    def test_physical_min_cost_uses_provider_object_geometry(self) -> None:
        _, _, material = compile_material(str(FIXTURE))

        retained = retained_module_paths(
            material,
            PhysicalHierarchyPolicy(PhysicalHierarchyMode.MIN_COST, 20000),
            PROVIDERS,
        )

        self.assertEqual(retained, frozenset({"module:test_with_inverters"}))


if __name__ == "__main__":
    unittest.main()
