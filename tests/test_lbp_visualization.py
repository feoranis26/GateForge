import unittest
from pathlib import Path

from gateforge.gateforge import compile_material
from gateforge.graph import MaterialGraph
from gateforge.hierarchy import PhysicalHierarchyMode, PhysicalHierarchyPolicy
from gateforge.placement import TopologicalPlacer
from gateforge.providers.lbp.common import LBP_PROVIDER
from gateforge.providers.lbp.objects import make_lbp_provider
from gateforge.visualization.build import build_visual_document


FIXTURE = Path(__file__).parents[1] / "scratch" / "test.v"
NESTED_FIXTURE = Path(__file__).parents[1] / "scratch" / "basic_nested.v"


class LBPVisualizationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.providers = {LBP_PROVIDER: make_lbp_provider()}
        _, _, cls.material = compile_material(
            str(FIXTURE),
            target_providers=cls.providers,
        )
        cls.graph = MaterialGraph.from_design(cls.material, cls.providers)
        cls.placed = TopologicalPlacer(providers=cls.providers).place(
            cls.graph
        ).finalize(cls.graph)

    def test_document_contains_material_and_realized_microchip_views(self) -> None:
        document = build_visual_document(
            self.material,
            self.graph,
            self.placed,
            self.providers,
        )

        self.assertEqual(
            {view.identifier for view in document.views},
            {"material", "lbp:realized"},
        )
        realized = next(view for view in document.views if view.identifier == "lbp:realized")
        self.assertEqual(len(realized.scenes), 2)
        self.assertEqual(realized.scenes[0].parent, None)
        self.assertIn("register-bank[9]", {scene.label for scene in realized.scenes})
        self.assertTrue(all(scene.elements for scene in realized.scenes))
        self.assertTrue(all(scene.nets for scene in realized.scenes))

    def test_realized_scene_uses_exact_placed_coordinates(self) -> None:
        document = build_visual_document(
            self.material,
            self.graph,
            self.placed,
            self.providers,
        )
        child = next(item for item in self.placed.containers if item.parent is not None)
        component = child.components[0]
        scene = next(
            item
            for item in next(
                view for view in document.views if view.identifier == "lbp:realized"
            ).scenes
            if item.identifier == child.path
        )
        element = scene.element(f"gadget:{component.identifier}")

        self.assertEqual(element.transform.x, component.x)
        self.assertEqual(element.transform.y, component.y)
        self.assertEqual(element.transform.angle, component.angle)

    def test_material_view_preserves_placed_module_hierarchy(self) -> None:
        _, _, material = compile_material(
            str(NESTED_FIXTURE),
            target_providers=self.providers,
        )
        graph = MaterialGraph.from_design(material, self.providers)
        placed = TopologicalPlacer(
            providers=self.providers,
            physical_hierarchy=PhysicalHierarchyPolicy(
                PhysicalHierarchyMode.PRESERVE_ALL
            ),
        ).place(graph).finalize(graph)

        document = build_visual_document(material, graph, placed, self.providers)
        view = next(item for item in document.views if item.identifier == "material")
        scenes = {item.identifier: item for item in view.scenes}

        self.assertEqual(set(scenes), {item.path for item in placed.containers})
        self.assertEqual(scenes[placed.root].parent, None)
        for child in placed.root_container.children:
            self.assertEqual(scenes[child].parent, placed.root)
            element = scenes[placed.root].element(f"container:{child}")
            self.assertEqual(element.linked_scene, child)
            self.assertEqual(element.transform.x, placed.container(child).x)
            self.assertEqual(element.transform.y, placed.container(child).y)


if __name__ == "__main__":
    unittest.main()