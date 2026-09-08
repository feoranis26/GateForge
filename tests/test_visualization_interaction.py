import unittest

from gateforge.target import PortDirection
from gateforge.visualization import (
    VisualBounds,
    VisualElement,
    VisualElementDescriptor,
    VisualPoint,
    VisualPort,
    VisualRectangle,
    VisualScene,
    VisualTransform,
)
from gateforge.visualization.interaction import (
    Viewport,
    find_collisions,
    flatten_point_coordinates,
    pick_element,
)


def _element(identifier: str, x: float, y: float) -> VisualElement:
    bounds = VisualBounds.centered(40.0, 20.0)
    return VisualElement(
        identifier,
        VisualElementDescriptor(
            identifier,
            bounds,
            (VisualRectangle(bounds),),
            (VisualPort("port", "P", PortDirection.INPUT, VisualPoint(-20.0, 0.0)),),
        ),
        VisualTransform(x, y),
    )


class VisualizationInteractionTests(unittest.TestCase):
    def test_canvas_flattens_points_for_tk_primitives(self) -> None:
        self.assertEqual(
            flatten_point_coordinates(
                (VisualPoint(1.0, 2.0), VisualPoint(3.0, 4.0))
            ),
            (1.0, 2.0, 3.0, 4.0),
        )

    def test_zoom_keeps_world_point_under_cursor(self) -> None:
        viewport = Viewport.fit(VisualBounds(-100.0, -50.0, 100.0, 50.0), 800, 600)
        focus = VisualPoint(215.0, 173.0)
        before = viewport.screen_to_world(focus, 800, 600)

        zoomed = viewport.zoom_at(1.5, focus, 800, 600)

        self.assertEqual(zoomed.screen_to_world(focus, 800, 600), before)

    def test_pick_and_collision_use_element_bounds(self) -> None:
        left = _element("left", 0.0, 0.0)
        overlapping = _element("overlapping", 30.0, 0.0)
        separate = _element("separate", 80.0, 0.0)
        scene = VisualScene(
            "scene",
            "Scene",
            VisualBounds(-100.0, -50.0, 120.0, 50.0),
            (left, overlapping, separate),
        )

        self.assertEqual(pick_element(scene, VisualPoint(5.0, 0.0)), left)
        self.assertEqual(find_collisions(scene), (("left", "overlapping"),))


if __name__ == "__main__":
    unittest.main()