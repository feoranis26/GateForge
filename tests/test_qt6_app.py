import os
import json
from pathlib import Path
import subprocess
import sys
import unittest
from tempfile import TemporaryDirectory
from unittest.mock import Mock, patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

try:
    from PySide6.QtCore import QEventLoop, QTimer, Qt
    from PySide6.QtWidgets import QApplication, QFileDialog
except ModuleNotFoundError as error:
    raise unittest.SkipTest("PySide6 workbench extra is not installed") from error

from gateforge.workbench.qt_app import FactorioInputsDialog, WorkbenchWindow
from gateforge.workbench.protocol import WorkerCommand


FIXTURE = Path(__file__).parent / "fixtures" / "single_not.v"
FACTORIO_FIXTURE = Path(__file__).parent / "fixtures" / "factorio" / "add32.v"


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

    def test_factorio_target_opens_with_backend_placement_defaults(self) -> None:
        window = WorkbenchWindow(target="factorio")
        self.addCleanup(window.close)

        self.assertEqual(window.target_combo.currentData(), "factorio")
        self._wait_for_operation(
            window,
            lambda: window.open_source(FACTORIO_FIXTURE),
        )

        self.assertEqual(window.snapshot["target"], "factorio")
        self.assertEqual(window.column_pitch_spin.value(), 6.0)
        self.assertEqual(window.row_pitch_spin.value(), 3.0)
        self.assertEqual(window.routing_group_height_spin.value(), 8.0)
        self.assertEqual(window.routing_gap_rows_spin.value(), 1)

    def test_factorio_realization_controls_build_one_provider_payload(self) -> None:
        window = WorkbenchWindow(target="factorio")
        self.addCleanup(window.close)
        window.add_input_combinators_check.setChecked(True)
        window.add_output_lamps_check.setChecked(True)
        window._factorio_input_values = {"a": "0xffffffff", "b": "2"}

        self.assertFalse(window.factorio_toolbar.isHidden())
        self.assertEqual(
            window._provider_options_payload(),
            {
                "factorio": {
                    "input_drivers": "constant",
                    "input_values": {"a": "0xffffffff", "b": "2"},
                    "output_lamps": True,
                    "power_layout": "grid",
                }
            },
        )

        window.target_combo.setCurrentIndex(window.target_combo.findData("lbp"))
        self.assertTrue(window.factorio_toolbar.isHidden())
        self.assertEqual(window._provider_options_payload(), {})

    def test_factorio_inputs_dialog_rejects_duplicate_ports(self) -> None:
        dialog = FactorioInputsDialog({"a": "1"})
        self.addCleanup(dialog.close)
        dialog._add_row("a", "2")

        with self.assertRaisesRegex(ValueError, "Duplicate.*a"):
            dialog.values()

    def test_hdl_interface_bindings_reach_workbench_inspector(self) -> None:
        window = WorkbenchWindow(target="factorio")
        self.addCleanup(window.close)
        self._wait_for_operation(window, lambda: window.open_source(FACTORIO_FIXTURE.parent / "bound_add32.v"))
        window.compile_material()
        self._wait_until(window, lambda: window._phase() == "materialized" and not window._busy)
        window.place()
        self._wait_until(window, lambda: window._document is not None and not window._busy)
        scene = window._document.views[0].scenes[0]
        interfaces = [{item.name: item.value for item in element.descriptor.properties if item.group == "Interface"} for element in scene.elements]
        interfaces = [item for item in interfaces if "Circuit" in item]
        self.assertEqual(len(interfaces), 2)
        by_circuit = {item["Circuit"]: item for item in interfaces}
        self.assertEqual(by_circuit["inputs"]["Port a"], "signal-A")
        self.assertEqual(by_circuit["inputs"]["Port b"], "signal-B")
        self.assertEqual(by_circuit["inputs"]["Color"], "green")
        self.assertEqual(by_circuit["outputs"]["Port y"], "signal-C")
        self.assertEqual(by_circuit["outputs"]["Port tap"], "signal-D")
        self.assertEqual(by_circuit["outputs"]["Color"], "red")
        self.assertTrue(window.action_export.isEnabled())

    def test_bitwise_operations_reach_workbench_inspector(self) -> None:
        window = WorkbenchWindow(target="factorio")
        self.addCleanup(window.close)
        self._wait_for_operation(window, lambda: window.open_source(FACTORIO_FIXTURE.parent / "bitwise32.v"))
        window.compile_material()
        self._wait_until(window, lambda: window._phase() == "materialized" and not window._busy)
        window.place()
        self._wait_until(window, lambda: window._document is not None and not window._busy)
        scene = window._document.views[0].scenes[0]
        operations = [item.value for element in scene.elements for item in element.descriptor.properties if item.name == "Operation"]
        self.assertEqual(sorted(operations), ["+", "AND", "OR", "XOR", "XOR"])
        self.assertTrue(window.action_export.isEnabled())

    def test_conditional_process_reaches_workbench_inspector(self) -> None:
        window = WorkbenchWindow(target="factorio")
        self.addCleanup(window.close)
        self._wait_for_operation(window, lambda: window.open_source(FACTORIO_FIXTURE.parent / "conditional32.v"))
        window.compile_material()
        self._wait_until(window, lambda: window._phase() == "materialized" and not window._busy)
        window.place()
        self._wait_until(window, lambda: window._document is not None and not window._busy)
        scene = window._document.views[0].scenes[0]
        properties = [{item.name: item.value for item in element.descriptor.properties} for element in scene.elements]
        deciders = [item for item in properties if item.get("Prototype") == "decider-combinator"]
        self.assertTrue(deciders)
        self.assertTrue(all(item["Role"] == "Decider" and item["True output"] == "1" for item in deciders))
        self.assertTrue(window.action_export.isEnabled())

    def test_factorio_option_change_rebuilds_realized_views(self) -> None:
        window = WorkbenchWindow(target="factorio")
        self.addCleanup(window.close)
        window.snapshot = {"phase": "realized", "artifacts": {}}

        with patch.object(window, "realize") as rebuild:
            window.add_output_lamps_check.setChecked(True)

        rebuild.assert_called_once_with()

    def test_factorio_workbench_owns_realization_and_rebuilds_options(self) -> None:
        window = WorkbenchWindow(target="factorio")
        self.addCleanup(window.close)
        self._wait_for_operation(window, lambda: window.open_source(FACTORIO_FIXTURE))
        window.compile_material()
        self._wait_until(window, lambda: window._phase() == "materialized" and not window._busy)
        with patch.object(window.worker, "request", wraps=window.worker.request) as requests:
            window.place()
            self._wait_until(window, lambda: window._phase() == "realized" and not window._busy and window._document is not None)
        commands = [call.args[0] for call in requests.call_args_list]
        self.assertEqual(commands.count(WorkerCommand.PLACE), 1)
        self.assertNotIn(WorkerCommand.REALIZE, commands)
        self.assertEqual(len(window._document.views), 1)
        self.assertEqual(window.content_tabs.tabText(window.content_tabs.indexOf(window._visual_tabs[0])), "Placement")
        scene = window._document.views[0].scenes[0]
        inventory = [element for element in scene.elements if element.collision_enabled]
        self.assertEqual(window.entities.topLevelItemCount(), len(inventory))
        pole_row = next(window.entities.topLevelItem(index) for index in range(window.entities.topLevelItemCount()) if "external pole" in window.entities.topLevelItem(index).text(2))
        self.assertEqual(pole_row.text(1), "medium-electric-pole")
        window.entities.setCurrentItem(pole_row)
        self.assertGreater(window.inspector.topLevelItemCount(), 0)
        self.assertTrue(window._scene_canvases[0].scene().selectedItems())
        self.assertTrue(window.action_realize.isVisible())
        self.assertEqual(window.realizations.topLevelItemCount(), 1)
        self.assertTrue(window.action_export.isEnabled())
        initial = window.snapshot["selected_realization"]
        self.assertIn("factorio:finalized", {view.identifier for view in window._document.views})
        window.add_output_lamps_check.setChecked(True)
        self.assertFalse(window.action_export.isEnabled())
        self.assertIsNone(window._document)
        self.assertEqual(window.entities.topLevelItemCount(), 0)
        self.assertFalse(window.save_actions["placement"].isEnabled())
        self._wait_until(window, lambda: window._phase() == "realized" and not window._busy and window._document is not None)
        self.assertNotEqual(window.snapshot["selected_realization"], initial)
        self.assertTrue(window.save_actions["realization"].isEnabled())
        item = window.realizations.topLevelItem(0)
        window._realization_selected(item)
        self.assertGreater(window.inspector.topLevelItemCount(), 0)
        grid_selection = window.snapshot["selected_realization"]
        window.power_layout_combo.setCurrentIndex(window.power_layout_combo.findData("compact"))
        self.assertFalse(window.action_export.isEnabled())
        self.assertIsNone(window._document)
        self._wait_until(window, lambda: window._phase() == "realized" and not window._busy and window._document is not None)
        self.assertNotEqual(window.snapshot["selected_realization"], grid_selection)
        self.assertTrue(all(item["details"]["power_layout"] == "compact" for item in window.snapshot["realizations"]))
        window.add_input_combinators_check.setChecked(True)
        self._wait_until(window, lambda: window._phase() == "realized" and not window._busy and window._document is not None)
        driver_rows = [window.entities.topLevelItem(index) for index in range(window.entities.topLevelItemCount()) if "constant driver" in window.entities.topLevelItem(index).text(2)]
        self.assertEqual(len(driver_rows), 2)
        self.assertTrue(all(row.text(1) == "constant-combinator" for row in driver_rows))

    def test_failed_realization_clears_stale_export_state(self) -> None:
        window = WorkbenchWindow(target="factorio")
        self.addCleanup(window.close)
        window.snapshot = {"phase": "realized", "supports_realization": True, "selected_realization": "old", "artifacts": {"placement": {}, "realization": {}}}
        with patch.object(window, "_run_worker"):
            window.realize()
        self.assertIsNone(window.snapshot["selected_realization"])
        window._task_failed(RuntimeError("No route"))
        self.assertFalse(window.action_export.isEnabled())
        self.assertTrue(window.action_realize.isEnabled())

    def test_chained_factorio_candidate_selection_controls_saved_and_exported_artifact(self) -> None:
        window = WorkbenchWindow(target="factorio")
        self.addCleanup(window.close)
        window.power_layout_combo.setCurrentIndex(window.power_layout_combo.findData("compact"))
        self._wait_for_operation(window, lambda: window.open_source(FACTORIO_FIXTURE.parent / "chained_add32.v"))
        window.compile_material()
        self._wait_until(window, lambda: window._phase() == "materialized" and not window._busy)
        window.place()
        self._wait_until(window, lambda: window._document is not None and not window._busy)
        self.assertEqual(window.realizations.topLevelItemCount(), 2)
        winner = window.snapshot["realization_winner"]
        alternative = next(item for item in window.snapshot["realizations"] if item["identifier"] != winner)
        row = next(window.realizations.topLevelItem(index) for index in range(2) if window.realizations.topLevelItem(index).data(0, Qt.ItemDataRole.UserRole) == alternative["identifier"])
        window.realizations.setCurrentItem(row)
        self._wait_until(window, lambda: window._document is not None and not window._busy and window.snapshot["selected_realization"] == alternative["identifier"])
        self.assertEqual(window.snapshot["realization_winner"], winner)
        scene = next(view for view in window._document.views if view.identifier == "factorio:finalized").scenes[0]
        self.assertEqual(sum(item.descriptor.label == "arithmetic-combinator" for item in scene.elements), alternative["details"]["combinators"])
        with TemporaryDirectory() as directory:
            saved = Path(directory) / "realization.json"
            with patch.object(QFileDialog, "getSaveFileName", return_value=(str(saved), "JSON files (*.json)")):
                self._wait_for_operation(window, lambda: window.save_artifact("realization"))
            exported = Path(directory) / "blueprint.json"
            with patch.object(QFileDialog, "getSaveFileName", return_value=(str(exported), "JSON files (*.json)")):
                self._wait_for_operation(window, window.export_target)
            artifact = json.loads(saved.read_text())
            self.assertEqual(artifact["power_layout"], "compact")
            blueprint = json.loads(exported.read_text())["blueprint"]
            self.assertEqual(len(artifact["design"]["entities"]), len(blueprint["entities"]))
            self.assertEqual(sum(item["name"] == "arithmetic-combinator" for item in blueprint["entities"]), alternative["details"]["combinators"])

    def test_factorio_export_uses_realization_options(self) -> None:
        worker = Mock()
        worker.alive = False
        window = WorkbenchWindow(target="factorio", worker=worker)
        self.addCleanup(window.close)
        window.source_path = FACTORIO_FIXTURE
        window.add_input_combinators_check.setChecked(True)
        window.add_output_lamps_check.setChecked(True)
        window._factorio_input_values = {"a": "0xffffffff", "b": "2"}
        output = "/tmp/add32.blueprint.json"

        with (
            patch.object(
                QFileDialog,
                "getSaveFileName",
                return_value=(output, "JSON files (*.json)"),
            ),
            patch.object(window, "_run_worker") as run_worker,
        ):
            window.export_target()

        operation = run_worker.call_args.args[1]
        operation()
        worker.request.assert_called_once_with(
            WorkerCommand.EXPORT_FACTORIO_BLUEPRINT,
            {
                "path": output,
                "label": "add32",
                "add_input_combinators": True,
                "input_values": {"a": "0xffffffff", "b": "2"},
                "add_output_lamps": True,
                "power_layout": "grid",
            },
        )

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