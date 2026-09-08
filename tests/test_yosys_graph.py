from pathlib import Path
from unittest.mock import patch
import unittest

from gateforge.compiler import design_preprocess
from gateforge.workbench.yosys_graph import (
    GraphvizUnavailableError,
    SchematicRenderOptions,
    UnknownSchematicModuleError,
    YosysSchematicRenderer,
)


FIXTURE = Path(__file__).parent / "fixtures" / "single_not.v"


class YosysSchematicTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.checkpoint = design_preprocess(str(FIXTURE)).checkpoint()

    def test_renders_and_caches_checkpoint_schematic(self) -> None:
        renderer = YosysSchematicRenderer()

        first = renderer.render(self.checkpoint, module="test")
        second = renderer.render(self.checkpoint, module="test")

        self.assertIs(second, first)
        self.assertEqual(renderer.cache_size, 1)
        self.assertIn('digraph "test"', first.dot)
        self.assertTrue(first.svg.lstrip().startswith("<?xml"))
        self.assertEqual(first.module, "test")
        self.assertIn("test", first.modules)

    def test_options_participate_in_cache_key(self) -> None:
        renderer = YosysSchematicRenderer()

        default = renderer.render(self.checkpoint, module="test")
        annotated = renderer.render(
            self.checkpoint,
            module="test",
            options=SchematicRenderOptions(show_widths=True),
        )

        self.assertNotEqual(default.cache_key, annotated.cache_key)
        self.assertEqual(renderer.cache_size, 2)

    def test_rejects_unknown_module(self) -> None:
        with self.assertRaisesRegex(UnknownSchematicModuleError, "no module"):
            YosysSchematicRenderer().render(
                self.checkpoint,
                module="missing",
            )

    def test_missing_graphviz_only_disables_schematic_rendering(self) -> None:
        with patch(
            "gateforge.workbench.yosys_graph.shutil.which",
            return_value=None,
        ):
            with self.assertRaisesRegex(GraphvizUnavailableError, "Graphviz"):
                YosysSchematicRenderer().render(self.checkpoint, module="test")


if __name__ == "__main__":
    unittest.main()