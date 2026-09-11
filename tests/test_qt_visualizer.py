import os
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import Mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

try:
    from PySide6.QtWidgets import QApplication
except ModuleNotFoundError as error:
    raise unittest.SkipTest("PySide6 workbench extra is not installed") from error

from gateforge.target import PortDirection
from gateforge.visualization import (
    VisualBounds,
    VisualDocument,
    VisualElement,
    VisualElementDescriptor,
    VisualEndpoint,
    VisualNet,
    VisualPoint,
    VisualPort,
    VisualProperty,
    VisualRectangle,
    VisualScene,
    VisualTransform,
    VisualView,
)
from gateforge.visualization.qt_app import VisualizerWindow


def _document(label: str = "Hierarchy") -> VisualDocument:
    element_bounds = VisualBounds.centered(40.0, 30.0)
    source = VisualElement(
        "source",
        VisualElementDescriptor(
            "Source",
            element_bounds,
            (VisualRectangle(element_bounds),),
            (
                VisualPort(
                    "out",
                    "OUT",
                    PortDirection.OUTPUT,
                    VisualPoint(20.0, 0.0),
                ),
            ),
            (VisualProperty("Kind", "source"),),
        ),
        VisualTransform(-40.0, 0.0),
        linked_scene="child",
    )
    sink = VisualElement(
        "sink",
        VisualElementDescriptor(
            "Sink",
            element_bounds,
            (VisualRectangle(element_bounds),),
            (
                VisualPort(
                    "in",
                    "IN",
                    PortDirection.INPUT,
                    VisualPoint(-20.0, 0.0),
                ),
            ),
        ),
        VisualTransform(40.0, 0.0),
    )
    root = VisualScene(
        "root",
        "Root",
        VisualBounds.centered(160.0, 100.0),
        (source, sink),
        (
            VisualNet(
                "signal",
                "Signal",
                (VisualEndpoint("source", "out"), VisualEndpoint("sink", "in")),
            ),
        ),
    )
    child = VisualScene(
        "child",
        "Child",
        VisualBounds.centered(80.0, 60.0),
        (sink,),
        parent="root",
    )
    other = VisualScene(
        "other",
        "Other",
        VisualBounds.centered(80.0, 60.0),
        (sink,),
    )
    return VisualDocument(
        (
            VisualView("hierarchy", label, (root, child)),
            VisualView("other", "Other view", (other,)),
        )
    )


class QtVisualizerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.application = QApplication.instance() or QApplication([])

    def test_window_switches_views_and_activates_linked_scenes(self) -> None:
        window = VisualizerWindow(_document())
        self.addCleanup(window.close)

        self.assertEqual(window.view_selector.count(), 2)
        self.assertEqual(window.scene_tabs.count(), 2)
        window._activate_scene("child")
        self.assertEqual(window.scene_tabs.tabText(window.scene_tabs.currentIndex()), "Child")

        window.view_selector.setCurrentIndex(
            window.view_selector.findData("other")
        )
        self.assertEqual(window.scene_tabs.count(), 1)
        self.assertEqual(window.scene_tabs.tabText(0), "Other")

    def test_overlays_and_selection_use_the_shared_qt_canvas(self) -> None:
        window = VisualizerWindow(_document())
        self.addCleanup(window.close)

        window.action_wires.setChecked(False)
        window.action_labels.setChecked(False)
        window.action_bounds.setChecked(True)
        window.action_collisions.setChecked(False)
        window._apply_overlays()
        self.assertTrue(
            all(
                not canvas.show_wires
                and not canvas.show_labels
                and canvas.show_bounds
                and not canvas.show_collisions
                for canvas in window._canvases.values()
            )
        )

        root = window._canvases["root"]
        root._element_items["source"].setSelected(True)
        self.application.processEvents()
        self.assertEqual(window.selection_label.text(), "Source")
        self.assertEqual(window.nets.count(), 1)

    def test_reload_and_file_watch_replace_the_document(self) -> None:
        with TemporaryDirectory(prefix="gateforge-qt-viewer-") as directory:
            watched = Path(directory) / "material.json"
            watched.write_text("{}", encoding="utf-8")
            reload_document = Mock(return_value=_document("Reloaded hierarchy"))
            window = VisualizerWindow(
                _document(),
                reload_document=reload_document,
                watch_paths=(watched,),
                watch=True,
            )
            self.addCleanup(window.close)

            window.reload()
            self.assertEqual(reload_document.call_count, 1)
            self.assertEqual(window.view_selector.currentText(), "Reloaded hierarchy")
            self.assertIn("Reloaded", window.statusBar().currentMessage())

            window._watch_signature = ()
            window._poll_files()
            self.assertEqual(reload_document.call_count, 2)


if __name__ == "__main__":
    unittest.main()