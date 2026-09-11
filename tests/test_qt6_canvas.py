import os
from pathlib import Path
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

try:
    from PySide6.QtCore import QRectF
    from PySide6.QtGui import QColor, QImage, QPainter
    from PySide6.QtWidgets import QApplication
except ModuleNotFoundError as error:
    raise unittest.SkipTest("PySide6 workbench extra is not installed") from error

from gateforge.target import PortDirection
from gateforge.visualization import (
    VisualBounds,
    VisualElement,
    VisualElementDescriptor,
    VisualEllipse,
    VisualEndpoint,
    VisualNet,
    VisualPoint,
    VisualPolygon,
    VisualPolyline,
    VisualPort,
    VisualRectangle,
    VisualScene,
    VisualText,
    VisualTransform,
)
from gateforge.workbench.qt_canvas import VisualSceneView, YosysSvgView
from gateforge.compiler import compilation_backend, materialize_search_candidates, start_compilation_search
from gateforge.providers.factorio.finalization import finalize_add_fabric
from gateforge.providers.factorio.realization import propose_add_fabrics
from gateforge.providers.factorio.visualization import build_finalized_factorio_view
from gateforge.session import CompilationSession
from gateforge.graph import MaterialGraph
from gateforge.placement import TopologicalPlacer
from gateforge.visualization.build import build_visual_document


class QtCanvasTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.application = QApplication.instance() or QApplication([])

    def test_svg_view_loads_nonempty_document(self) -> None:
        view = YosysSvgView()

        view.load_svg(
            '<svg xmlns="http://www.w3.org/2000/svg" width="100" height="60">'
            '<rect width="100" height="60" fill="#20827f"/></svg>'
        )

        self.assertEqual(len(view.scene().items()), 1)
        self.assertFalse(view.scene().itemsBoundingRect().isEmpty())

    def test_factorio_baseline_labels_fit_nonoverlapping_footprints(self) -> None:
        session = CompilationSession(Path(__file__).parent / "fixtures" / "factorio" / "add32.v", target="factorio")
        material = session.run_to_material()
        graph = MaterialGraph.from_design(material, session.target_providers)
        placement = TopologicalPlacer(session.default_placement_options, providers=session.target_providers).place(graph).finalize(graph)
        scene = build_visual_document(material, graph, placement, session.target_providers, realized_views=()).views[0].scenes[0]
        self.assertTrue(all(not first.world_bounds.intersects(second.world_bounds) for index, first in enumerate(scene.elements) for second in scene.elements[index + 1:]))
        view = VisualSceneView(scene)
        self.addCleanup(view.close)
        by_id = {item.identifier: item for item in scene.elements}
        for identifier, group in view._element_items.items():
            bounds = by_id[identifier].descriptor.bounds
            rectangle = QRectF(bounds.min_x, bounds.min_y, bounds.width, bounds.height)
            for label in view._label_items:
                if label.parentItem() == group:
                    self.assertTrue(rectangle.contains(label.mapRectToParent(label.boundingRect())))
        for width, height in ((960, 540), (360, 480)):
            image = QImage(width, height, QImage.Format.Format_ARGB32)
            image.fill(QColor("#ffffff"))
            painter = QPainter(image)
            view.scene().render(painter, QRectF(0, 0, width, height))
            painter.end()
            self.assertGreater(len({image.pixelColor(column, row).rgba() for column in range(0, width, 4) for row in range(0, height, 4)}), 5)

    def test_finalized_factorio_inventory_renders_to_pixels_without_overlaps(self) -> None:
        backend = compilation_backend("factorio")
        path = Path(__file__).parent / "fixtures" / "factorio" / "add32.v"
        search = start_compilation_search(str(path), target="factorio", behavior_lowerer=backend.behavior_lowerer).finish()
        baseline = materialize_search_candidates(search, backend.target_providers).candidates[0]
        assert baseline.behavior is not None
        fabric = propose_add_fabrics(baseline.material, baseline.behavior)[0]
        finalized = finalize_add_fabric(fabric, baseline.material, baseline.behavior.graph, input_drivers="constant", output_lamps=True)
        scene = build_finalized_factorio_view(finalized).scenes[0]
        entity_elements = [item for item in scene.elements if item.identifier.startswith("factorio-entity:")]
        self.assertEqual(len(entity_elements), len(finalized.entities))
        self.assertTrue(all(not left.world_bounds.intersects(right.world_bounds) for index, left in enumerate(entity_elements) for right in entity_elements[index + 1:]))
        view = VisualSceneView(scene)
        view.set_overlays(wires=True, labels=False, bounds=False, collisions=False)
        for width, height in ((640, 360), (320, 480)):
            image = QImage(width, height, QImage.Format.Format_ARGB32)
            image.fill(QColor("#ffffff"))
            painter = QPainter(image)
            view.scene().render(painter, QRectF(0, 0, width, height))
            painter.end()
            colors = {image.pixelColor(column, row).rgba() for column in range(0, width, 4) for row in range(0, height, 4)}
            self.assertGreater(len(colors), 5)
        view.close()

    def test_visual_scene_renders_all_primitive_types_to_pixels(self) -> None:
        bounds = VisualBounds.centered(120.0, 80.0)
        element = VisualElement(
            "mixed",
            VisualElementDescriptor(
                "Mixed",
                bounds,
                (
                    VisualRectangle(bounds),
                    VisualEllipse(VisualBounds.centered(70.0, 40.0)),
                    VisualPolyline(
                        (VisualPoint(-40.0, 0.0), VisualPoint(40.0, 0.0))
                    ),
                    VisualPolygon(
                        (
                            VisualPoint(-10.0, 10.0),
                            VisualPoint(10.0, 10.0),
                            VisualPoint(0.0, -10.0),
                        )
                    ),
                    VisualText(VisualPoint(0.0, 0.0), "Gate"),
                ),
                (
                    VisualPort(
                        "in",
                        "IN",
                        PortDirection.INPUT,
                        VisualPoint(-60.0, 0.0),
                    ),
                ),
            ),
            VisualTransform(0.0, 0.0),
        )
        peer = VisualElement(
            "peer",
            VisualElementDescriptor(
                "Peer",
                VisualBounds.centered(30.0, 30.0),
                (VisualRectangle(VisualBounds.centered(30.0, 30.0)),),
                (
                    VisualPort(
                        "out",
                        "OUT",
                        PortDirection.OUTPUT,
                        VisualPoint(15.0, 0.0),
                    ),
                ),
            ),
            VisualTransform(-20.0, 0.0),
        )
        scene = VisualScene(
            "root",
            "Root",
            bounds,
            (element, peer),
            (
                VisualNet(
                    "net",
                    "Signal",
                    (VisualEndpoint("peer", "out"), VisualEndpoint("mixed", "in")),
                ),
            ),
            grid_size=10.0,
        )
        view = VisualSceneView(scene)
        view.resize(480, 320)
        view.set_overlays(wires=True, labels=False, bounds=True, collisions=True)
        view.highlight_net("net")

        self.assertTrue(all(item.isVisible() for item in view._net_items["net"]))
        self.assertTrue(all(not item.isVisible() for item in view._label_items))
        self.assertTrue(all(item.isVisible() for item in view._bound_items))
        self.assertTrue(all(item.isVisible() for item in view._collision_items))
        self.assertEqual(view._net_items["net"][0].pen().widthF(), 3.0)
        self.assertNotEqual(
            view._net_items["net"][0].pen().color(),
            QColor("#427a78"),
        )

        view.set_overlays(wires=False, labels=True, bounds=False, collisions=False)
        self.assertTrue(all(not item.isVisible() for item in view._net_items["net"]))
        self.assertTrue(all(item.isVisible() for item in view._label_items))
        self.assertTrue(all(not item.isVisible() for item in view._bound_items))
        self.assertTrue(all(not item.isVisible() for item in view._collision_items))
        view.set_overlays(wires=True, labels=False, bounds=True, collisions=True)

        image = QImage(480, 320, QImage.Format.Format_ARGB32)
        image.fill(QColor("#f4f3ef"))
        painter = QPainter(image)
        view.scene().render(painter, QRectF(0, 0, 480, 320))
        painter.end()

        colors = {
            image.pixelColor(x, y).rgba()
            for x in range(0, image.width(), 8)
            for y in range(0, image.height(), 8)
        }
        self.assertGreater(len(view.scene().items()), 5)
        self.assertGreater(len(colors), 1)
        self.assertFalse(view.show_labels)
        self.assertTrue(view.show_bounds)
        self.assertTrue(view.show_collisions)

    def test_highlighted_nets_use_distinct_colors_and_offset_routes(self) -> None:
        bounds = VisualBounds.centered(240.0, 160.0)
        gate_bounds = VisualBounds.centered(40.0, 40.0)
        source = VisualElement(
            "source",
            VisualElementDescriptor(
                "Source",
                gate_bounds,
                (VisualRectangle(gate_bounds),),
                (
                    VisualPort(
                        "out-a",
                        "A",
                        PortDirection.OUTPUT,
                        VisualPoint(20.0, -8.0),
                    ),
                    VisualPort(
                        "out-b",
                        "B",
                        PortDirection.OUTPUT,
                        VisualPoint(20.0, 8.0),
                    ),
                ),
            ),
            VisualTransform(-80.0, 0.0),
        )
        sinks = tuple(
            VisualElement(
                f"sink-{index}",
                VisualElementDescriptor(
                    f"Sink {index}",
                    gate_bounds,
                    (VisualRectangle(gate_bounds),),
                    (
                        VisualPort(
                            "in",
                            "IN",
                            PortDirection.INPUT,
                            VisualPoint(-20.0, 0.0),
                        ),
                    ),
                ),
                VisualTransform(80.0, y),
            )
            for index, y in enumerate((-30.0, 30.0))
        )
        idle = VisualElement(
            "idle",
            VisualElementDescriptor(
                "Idle",
                gate_bounds,
                (VisualRectangle(gate_bounds),),
            ),
            VisualTransform(0.0, 60.0),
        )
        scene = VisualScene(
            "root",
            "Root",
            bounds,
            (source, *sinks, idle),
            (
                VisualNet(
                    "alpha",
                    "Alpha",
                    (
                        VisualEndpoint("source", "out-a"),
                        VisualEndpoint("sink-0", "in"),
                    ),
                ),
                VisualNet(
                    "beta",
                    "Beta",
                    (
                        VisualEndpoint("source", "out-b"),
                        VisualEndpoint("sink-1", "in"),
                    ),
                ),
            ),
        )
        view = VisualSceneView(scene)
        original_paths = {
            identifier: tuple(item.path() for item in items)
            for identifier, items in view._net_items.items()
        }

        view._element_items["source"].setSelected(True)

        alpha = view._net_items["alpha"]
        beta = view._net_items["beta"]
        self.assertNotEqual(alpha[0].pen().color(), beta[0].pen().color())
        self.assertTrue(all(item.pen().widthF() == 3.0 for item in (*alpha, *beta)))
        self.assertNotEqual(alpha[0].path(), original_paths["alpha"][0])
        self.assertNotEqual(beta[0].path(), original_paths["beta"][0])
        self.assertEqual(view._element_items["source"].opacity(), 1.0)
        self.assertEqual(view._element_items["idle"].opacity(), 0.28)
        for identifier, items in (("alpha", alpha), ("beta", beta)):
            for item, original in zip(items, original_paths[identifier]):
                self.assertEqual(item.path().elementAt(0), original.elementAt(0))

        colors = (alpha[0].pen().color(), beta[0].pen().color())
        view.highlight_nets(set())
        view.highlight_nets({"beta", "alpha"})
        self.assertEqual(
            colors,
            (alpha[0].pen().color(), beta[0].pen().color()),
        )


if __name__ == "__main__":
    unittest.main()