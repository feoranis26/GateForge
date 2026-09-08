import unittest

from gateforge.target import PortDirection
from gateforge.visualization import (
    VisualBounds,
    VisualDocument,
    VisualEllipse,
    VisualElement,
    VisualElementDescriptor,
    VisualEndpoint,
    VisualNet,
    VisualPoint,
    VisualPolygon,
    VisualPolyline,
    VisualPort,
    VisualProperty,
    VisualRectangle,
    VisualReference,
    VisualScene,
    VisualStyle,
    VisualText,
    VisualTransform,
    VisualView,
)
from gateforge.visualization.model import VisualizationError


def _element(identifier: str, x: float = 0.0, angle: float = 0.0) -> VisualElement:
    bounds = VisualBounds.centered(40.0, 20.0)
    return VisualElement(
        identifier,
        VisualElementDescriptor(
            label=identifier,
            bounds=bounds,
            primitives=(VisualRectangle(bounds),),
            ports=(
                VisualPort("in", "IN", PortDirection.INPUT, VisualPoint(-20.0, 0.0)),
                VisualPort("out", "OUT", PortDirection.OUTPUT, VisualPoint(20.0, 0.0)),
            ),
        ),
        VisualTransform(x, 0.0, angle),
    )


class VisualizationModelTests(unittest.TestCase):
    def test_rotated_element_reports_world_bounds_and_ports(self) -> None:
        element = _element("gate", 10.0, 90.0)

        self.assertAlmostEqual(element.world_bounds.min_x, 0.0)
        self.assertAlmostEqual(element.world_bounds.max_x, 20.0)
        self.assertAlmostEqual(element.world_bounds.min_y, -20.0)
        self.assertAlmostEqual(element.world_bounds.max_y, 20.0)
        output = element.port_position("out")
        self.assertAlmostEqual(output.x, 10.0)
        self.assertAlmostEqual(output.y, 20.0)

    def test_scene_validates_and_indexes_connected_elements(self) -> None:
        source = _element("source", -50.0)
        target = _element("target", 50.0)
        net = VisualNet(
            "net",
            "signal",
            (VisualEndpoint("source", "out"), VisualEndpoint("target", "in")),
        )
        scene = VisualScene(
            "root",
            "Root",
            VisualBounds(-100.0, -50.0, 100.0, 50.0),
            (target, source),
            (net,),
        )
        document = VisualDocument((VisualView("material", "Material", (scene,)),))

        self.assertEqual(document.views[0].scenes[0].elements, (source, target))
        self.assertEqual(scene.nets_for_element("source"), (net,))

    def test_scene_rejects_unknown_net_endpoint(self) -> None:
        with self.assertRaisesRegex(VisualizationError, "unknown element"):
            VisualScene(
                "root",
                "Root",
                VisualBounds(-100.0, -50.0, 100.0, 50.0),
                (_element("source"),),
                (
                    VisualNet(
                        "net",
                        "signal",
                        (
                            VisualEndpoint("source", "out"),
                            VisualEndpoint("missing", "in"),
                        ),
                    ),
                ),
            )

    def test_view_rejects_element_link_to_unknown_scene(self) -> None:
        bounds = VisualBounds.centered(20.0, 20.0)
        element = VisualElement(
            "nested",
            VisualElementDescriptor("Nested", bounds, (VisualRectangle(bounds),)),
            VisualTransform(0.0, 0.0),
            linked_scene="missing",
        )
        scene = VisualScene("root", "Root", bounds, (element,))

        with self.assertRaisesRegex(VisualizationError, "unknown scene"):
            VisualView("view", "View", (scene,))

    def test_canonical_document_round_trip_preserves_all_primitives(self) -> None:
        bounds = VisualBounds.centered(40.0, 20.0)
        style = VisualStyle("#ffffff", "#123456", 2.0, (3, 2))
        element = VisualElement(
            "shape",
            VisualElementDescriptor(
                "Shape",
                bounds,
                (
                    VisualRectangle(bounds, style),
                    VisualEllipse(bounds, style),
                    VisualPolyline(
                        (VisualPoint(-1.0, 0.0), VisualPoint(1.0, 0.0)),
                        style,
                    ),
                    VisualPolygon(
                        (
                            VisualPoint(-1.0, 1.0),
                            VisualPoint(1.0, 1.0),
                            VisualPoint(0.0, -1.0),
                        ),
                        style,
                    ),
                    VisualText(VisualPoint(0.0, 0.0), "Label"),
                ),
                (
                    VisualPort(
                        "in",
                        "IN",
                        PortDirection.INPUT,
                        VisualPoint(-20.0, 0.0),
                    ),
                    VisualPort(
                        "out",
                        "OUT",
                        PortDirection.OUTPUT,
                        VisualPoint(20.0, 0.0),
                    ),
                ),
                (VisualProperty("Type", "test", "Identity"),),
            ),
            VisualTransform(12.0, 18.0, 90.0),
            (VisualReference("object", "shape"),),
            layer=2,
            collision_enabled=False,
        )
        peer = _element("peer", 80.0)
        net = VisualNet(
            "net",
            "Signal",
            (VisualEndpoint("shape", "out"), VisualEndpoint("peer", "in")),
            (VisualProperty("Fanout", "1"),),
        )
        document = VisualDocument(
            (
                VisualView(
                    "material",
                    "Material",
                    (
                        VisualScene(
                            "root",
                            "Root",
                            VisualBounds(-100.0, -100.0, 100.0, 100.0),
                            (element, peer),
                            (net,),
                            grid_size=10.0,
                        ),
                    ),
                ),
            )
        )

        restored = VisualDocument.from_canonical_data(document.canonical_data())

        self.assertEqual(restored, document)

    def test_canonical_document_rejects_unknown_fields(self) -> None:
        document = VisualDocument(
            (
                VisualView(
                    "view",
                    "View",
                    (VisualScene("root", "Root", VisualBounds.centered(1, 1), ()),),
                ),
            )
        )
        data = document.canonical_data()
        data["unexpected"] = True

        with self.assertRaisesRegex(VisualizationError, "unknown"):
            VisualDocument.from_canonical_data(data)


if __name__ == "__main__":
    unittest.main()