import os
from pathlib import Path
import subprocess
import sys
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

try:
    from PySide6.QtCore import QEventLoop, QTimer, Qt
    from PySide6.QtWidgets import QApplication
except ModuleNotFoundError as error:
    raise unittest.SkipTest("PySide6 workbench extra is not installed") from error

from gateforge.workbench.qt_app import WorkbenchWindow


FIXTURE = Path(__file__).parent / "fixtures" / "single_not.v"


class QtWorkbenchTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.application = QApplication.instance() or QApplication([])

    def test_importing_qt_app_does_not_import_pyosys(self) -> None:
        result = subprocess.run(
            [
                sys.executable,
                "-c",
                (
                    "import sys; import gateforge.workbench.qt_app; "
                    "assert 'pyosys' not in sys.modules"
                ),
            ],
            check=False,
            capture_output=True,
            text=True,
        )

        self.assertEqual(result.returncode, 0, result.stderr)

    def test_shell_opens_source_and_preprocesses_in_spawned_worker(self) -> None:
        window = WorkbenchWindow()
        self.addCleanup(window.close)

        self._wait_for_operation(window, lambda: window.open_source(FIXTURE))

        self.assertIn("assign y = !a", window.source_editor.toPlainText())
        self.assertEqual(window.snapshot["phase"], "source-loaded")
        self.assertTrue(window.action_next.isEnabled())

        self._wait_for_operation(window, window.next_stage)

        self.assertEqual(window.snapshot["phase"], "preprocessed")
        self.assertGreater(window.candidates.model().rowCount(), 0)
        self.assertFalse(window.source_editor.isReadOnly() is False)

    def test_shell_exposes_expected_workbench_commands(self) -> None:
        window = WorkbenchWindow()
        self.addCleanup(window.close)

        names = {
            action.objectName()
            for action in window.findChildren(type(window.action_open))
        }

        self.assertTrue(
            {
                "actionOpen",
                "actionReload",
                "actionNextStage",
                "actionContinue",
                "actionStop",
                "actionCompileMaterial",
                "actionPlace",
                "actionExportLbp",
                "actionWires",
                "actionLabels",
                "actionBounds",
                "actionCollisions",
            }.issubset(names)
        )
        self.assertEqual(
            [window.content_tabs.tabText(index) for index in range(2)],
            ["Yosys", "Material"],
        )

    def test_placement_controls_expose_hierarchy_and_layout_policy(self) -> None:
        window = WorkbenchWindow()
        self.addCleanup(window.close)
        window.source_path = FIXTURE
        window.snapshot = {"phase": "materialized", "artifacts": {}}

        window.physical_hierarchy_combo.setCurrentText("min-objects")
        window.generated_hierarchy_combo.setCurrentText("all")
        window.column_pitch_spin.setValue(315.0)
        window._update_actions()

        self.assertTrue(window.hierarchy_threshold_spin.isEnabled())
        self.assertEqual(window.physical_hierarchy_combo.currentText(), "min-objects")
        self.assertEqual(window.generated_hierarchy_combo.currentText(), "all")
        self.assertEqual(window.column_pitch_spin.value(), 315.0)
        window.physical_hierarchy_combo.setCurrentText("preserve-all")
        self.assertFalse(window.hierarchy_threshold_spin.isEnabled())

    def test_complete_workbench_flow_builds_schematic_and_realized_views(self) -> None:
        window = WorkbenchWindow()
        self.addCleanup(window.close)
        self._wait_for_operation(window, lambda: window.open_source(FIXTURE))

        window.continue_search()
        self._wait_until(
            window,
            lambda: window.snapshot is not None
            and window.snapshot["phase"] == "search-complete"
            and not window._busy
            and not window._continuing,
        )
        self._wait_until(window, lambda: bool(window.yosys_view.scene().items()))

        window.compile_material()
        self._wait_until(
            window,
            lambda: window.snapshot is not None
            and window.snapshot["phase"] == "materialized"
            and not window._busy,
        )
        window.place()
        self._wait_until(
            window,
            lambda: window.snapshot is not None
            and window.snapshot["phase"] == "realized"
            and not window._busy,
        )

        labels = {
            window.content_tabs.tabText(index)
            for index in range(window.content_tabs.count())
        }
        self.assertIn("Placement", labels)
        self.assertIn("LBP realization", labels)
        self.assertGreater(window.claims.model().rowCount(), 0)
        self.assertTrue(window._scene_canvases)
        window.action_wires.setChecked(False)
        window._apply_overlays()
        self.assertTrue(
            all(not canvas.show_wires for canvas in window._scene_canvases)
        )
        canvas = window._scene_canvases[0]
        scene = canvas.visual_scene
        self.assertIsNotNone(scene)
        assert scene is not None
        element = next(
            item for item in scene.elements if scene.nets_for_element(item.identifier)
        )
        net = scene.nets_for_element(element.identifier)[0]
        window._visual_element_selected(scene, canvas, element.identifier)
        inspector_root = window.inspector.topLevelItem(0)
        net_group = next(
            inspector_root.child(index)
            for index in range(inspector_root.childCount())
            if inspector_root.child(index).text(0) == "Connected net"
        )
        net_item = net_group.child(0)
        self.assertEqual(net_item.data(0, Qt.ItemDataRole.UserRole), net.identifier)
        window.inspector.setCurrentItem(net_item)
        self.assertEqual(canvas._net_items[net.identifier][0].pen().widthF(), 3.0)

    def _wait_for_operation(self, window: WorkbenchWindow, action) -> None:
        loop = QEventLoop()
        timed_out = []

        def timeout() -> None:
            timed_out.append(True)
            loop.quit()

        window.operation_finished.connect(loop.quit)
        QTimer.singleShot(15_000, timeout)
        action()
        loop.exec()
        window.operation_finished.disconnect(loop.quit)
        self.assertFalse(timed_out, window.statusBar().currentMessage())

    def _wait_until(self, window: WorkbenchWindow, condition) -> None:
        loop = QEventLoop()
        timed_out = []
        poll = QTimer()
        poll.setInterval(10)

        def check() -> None:
            if condition():
                loop.quit()

        def timeout() -> None:
            timed_out.append(True)
            loop.quit()

        poll.timeout.connect(check)
        poll.start()
        QTimer.singleShot(15_000, timeout)
        check()
        if not condition():
            loop.exec()
        poll.stop()
        self.assertFalse(timed_out, window.statusBar().currentMessage())


if __name__ == "__main__":
    unittest.main()