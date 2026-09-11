from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from gateforge.gateforge import _launch_visualizer, main


FIXTURE = Path(__file__).parents[1] / "scratch" / "basic.v"
NESTED_FIXTURE = Path(__file__).parents[1] / "scratch" / "basic_nested.v"
FACTORIO_FIXTURE = Path(__file__).parent / "fixtures" / "factorio" / "add32.v"


class CliTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.directory = TemporaryDirectory(prefix="gateforge-cli-test-")
        root = Path(cls.directory.name)
        cls.material_path = root / "nested" / "material.json"
        cls.one_shot_path = root / "nested" / "one-shot-placement.json"
        cls.factorio_material_path = root / "factorio" / "material.json"
        cls.factorio_placement_path = root / "factorio" / "placement.json"
        with redirect_stdout(StringIO()), redirect_stderr(StringIO()):
            main(
                [
                    "compile",
                    str(FIXTURE),
                    "--emit-material",
                    str(cls.material_path),
                    "--emit-placement",
                    str(cls.one_shot_path),
                ]
            )
            main(
                [
                    "compile",
                    str(FACTORIO_FIXTURE),
                    "--target",
                    "factorio",
                    "--emit-material",
                    str(cls.factorio_material_path),
                    "--emit-placement",
                    str(cls.factorio_placement_path),
                ]
            )

    @classmethod
    def tearDownClass(cls) -> None:
        cls.directory.cleanup()

    def test_compile_creates_parent_directories_and_both_artifacts(self) -> None:
        self.assertTrue(self.material_path.is_file())
        self.assertTrue(self.one_shot_path.is_file())

    def test_factorio_target_compiles_with_compact_placement_defaults(self) -> None:
        material_data = json.loads(self.factorio_material_path.read_text())
        placement_data = json.loads(self.factorio_placement_path.read_text())
        self.assertEqual(len(material_data["objects"]), 1)
        self.assertEqual(len(material_data["nets"]), 3)
        self.assertEqual(placement_data["target"], "factorio")
        self.assertEqual(placement_data["options"]["column_pitch"], 6.0)
        self.assertEqual(placement_data["options"]["row_pitch"], 3.0)

    def test_compile_emits_mapping_search_report(self) -> None:
        report_path = Path(self.directory.name) / "search" / "report.json"
        with redirect_stdout(StringIO()), redirect_stderr(StringIO()):
            main(
                [
                    "compile",
                    str(FIXTURE),
                    "--mapping-search",
                    "beam",
                    "--emit-search-report",
                    str(report_path),
                ]
            )

        report = json.loads(report_path.read_text())
        self.assertEqual(report["schema_version"], 1)
        self.assertEqual(len(report["stages"]), 4)
        self.assertIsInstance(report["winner"], str)
        self.assertTrue(report["terminal_candidates"])
        self.assertIn(
            "provider_object_cost",
            report["terminal_candidates"][0]["score"],
        )

    def test_reloaded_placement_matches_one_shot_output(self) -> None:
        reloaded = Path(self.directory.name) / "reloaded.json"
        with redirect_stdout(StringIO()), redirect_stderr(StringIO()):
            main(["place", str(self.material_path), "--output", str(reloaded)])

        self.assertEqual(reloaded.read_bytes(), self.one_shot_path.read_bytes())

    def test_place_stdout_is_json_and_status_uses_stderr(self) -> None:
        stdout = StringIO()
        stderr = StringIO()

        with redirect_stdout(stdout), redirect_stderr(stderr):
            main(["place", str(self.material_path)])

        value = json.loads(stdout.getvalue())
        self.assertEqual(value["schema_version"], 1)
        self.assertIn("Placed", stderr.getvalue())
        self.assertNotIn("Placed", stdout.getvalue())

    def test_compile_placement_requires_material_output(self) -> None:
        stderr = StringIO()

        with redirect_stderr(stderr), self.assertRaises(SystemExit) as raised:
            main(
                [
                    "compile",
                    str(FIXTURE),
                    "--emit-placement",
                    str(Path(self.directory.name) / "orphan.json"),
                ]
            )

        self.assertEqual(raised.exception.code, 1)
        self.assertIn("--emit-placement requires --emit-material", stderr.getvalue())

    def test_old_flat_cli_is_rejected(self) -> None:
        with redirect_stderr(StringIO()), self.assertRaises(SystemExit) as raised:
            main([str(FIXTURE)])

        self.assertEqual(raised.exception.code, 2)

    @patch("gateforge.visualization.qt_app.launch_visualizer")
    def test_visualizer_launcher_dispatches_directly_to_qt(self, launch) -> None:
        document = object()

        _launch_visualizer(document, watch=False)

        launch.assert_called_once_with(document, watch=False)

    def test_visualizer_launcher_reports_missing_pyside6(self) -> None:
        original_import = __import__

        def missing_pyside(name, *args, **kwargs):
            if name == "gateforge.visualization.qt_app":
                raise ModuleNotFoundError(
                    "No module named 'PySide6'",
                    name="PySide6",
                )
            return original_import(name, *args, **kwargs)

        with patch("builtins.__import__", side_effect=missing_pyside):
            with self.assertRaisesRegex(RuntimeError, "PySide6.*workbench"):
                _launch_visualizer(object())

    def test_lbp_toolkit_export_uses_paired_artifacts(self) -> None:
        output = Path(self.directory.name) / "export" / "object.json"
        stdout = StringIO()
        with redirect_stdout(stdout), redirect_stderr(StringIO()):
            main(
                [
                    "export",
                    "lbp-toolkit",
                    str(self.material_path),
                    str(self.one_shot_path),
                    "--output",
                    str(output),
                ]
            )

        value = json.loads(output.read_text())
        root = value["resource"]["things"][0]
        details = value["resource"]["inventoryData"]["userCreatedDetails"]
        self.assertEqual(value["revision"], 35128313)
        self.assertEqual(root["PSwitch"]["type"], "MICROCHIP")
        self.assertEqual(details["name"], "test")
        self.assertIn("6 material objects", details["description"])
        self.assertIn("Exported 6 material gadgets", stdout.getvalue())

    def test_factorio_blueprint_export_writes_direct_deterministic_json(self) -> None:
        first = Path(self.directory.name) / "factorio" / "blueprint-first.json"
        second = Path(self.directory.name) / "factorio" / "blueprint-second.json"
        arguments = [
            "export",
            "factorio-blueprint",
            str(self.factorio_material_path),
            str(self.factorio_placement_path),
            "--label",
            "GateForge add32",
            "--add-input-combinators",
            "--input-value",
            "a=0xffffffff",
            "--input-value",
            "b=2",
            "--add-output-lamps",
        ]

        stdout = StringIO()
        with redirect_stdout(stdout), redirect_stderr(StringIO()):
            main([*arguments, "--output", str(first)])
            main([*arguments, "--output", str(second)])

        data = json.loads(first.read_text())
        blueprint = data["blueprint"]
        entities = blueprint["entities"]
        self.assertEqual(first.read_bytes(), second.read_bytes())
        self.assertEqual(blueprint["version"], 562949958467584)
        self.assertEqual(blueprint["label"], "GateForge add32")
        self.assertEqual(
            [item["name"] for item in entities].count("constant-combinator"),
            2,
        )
        self.assertEqual(
            [item["name"] for item in entities].count("small-lamp"),
            1,
        )
        counts = {
            item["control_behavior"]["sections"]["sections"][0]["filters"][0][
                "count"
            ]
            for item in entities
            if item["name"] == "constant-combinator"
        }
        self.assertEqual(counts, {-1, 2})
        self.assertIn("Exported Factorio blueprint", stdout.getvalue())

    def test_factorio_blueprint_export_rejects_lbp_artifacts(self) -> None:
        output = Path(self.directory.name) / "must-not-be-factorio.json"
        stderr = StringIO()

        with redirect_stderr(stderr), self.assertRaises(SystemExit) as raised:
            main(
                [
                    "export",
                    "factorio-blueprint",
                    str(self.material_path),
                    str(self.one_shot_path),
                    "--output",
                    str(output),
                ]
            )

        self.assertEqual(raised.exception.code, 1)
        self.assertIn("requires a Factorio design", stderr.getvalue())
        self.assertFalse(output.exists())

    @patch("gateforge.gateforge._launch_visualizer")
    def test_visualize_builds_registered_provider_views_without_opening_tk(
        self,
        launch_visualizer,
    ) -> None:
        with redirect_stdout(StringIO()), redirect_stderr(StringIO()):
            main(
                [
                    "visualize",
                    str(self.material_path),
                    str(self.one_shot_path),
                    "--watch",
                ]
            )

        document = launch_visualizer.call_args.args[0]
        self.assertEqual(
            {view.identifier for view in document.views},
            {"material", "lbp:realized"},
        )
        self.assertTrue(launch_visualizer.call_args.kwargs["watch"])
        self.assertEqual(
            launch_visualizer.call_args.kwargs["watch_paths"],
            (self.material_path, self.one_shot_path),
        )

    @patch("gateforge.gateforge._launch_visualizer")
    def test_visualize_adds_named_factorio_input_combinators(
        self,
        launch_visualizer,
    ) -> None:
        with redirect_stdout(StringIO()), redirect_stderr(StringIO()):
            main(
                [
                    "visualize",
                    str(self.factorio_material_path),
                    str(self.factorio_placement_path),
                    "--add-input-combinators",
                    "--input-value",
                    "a=0xffffffff",
                    "--input-value",
                    "b=2",
                ]
            )

        document = launch_visualizer.call_args.args[0]
        views = {item.identifier: item for item in document.views}
        placed = views["factorio:placed"].scenes[0]
        self.assertEqual(
            len(
                [
                    item
                    for item in placed.elements
                    if "factorio:input-driver:" in item.identifier
                ]
            ),
            2,
        )

    def test_visualize_input_value_requires_input_combinators(self) -> None:
        stderr = StringIO()
        with redirect_stderr(stderr), self.assertRaises(SystemExit) as raised:
            main(
                [
                    "visualize",
                    str(self.factorio_material_path),
                    str(self.factorio_placement_path),
                    "--input-value",
                    "a=1",
                ]
            )

        self.assertEqual(raised.exception.code, 1)
        self.assertIn(
            "--input-value requires --add-input-combinators",
            stderr.getvalue(),
        )

    def test_lbp_toolkit_export_applies_metadata_overrides_deterministically(self) -> None:
        first = Path(self.directory.name) / "first-object.json"
        second = Path(self.directory.name) / "second-object.json"
        arguments = [
            "export",
            "lbp-toolkit",
            str(self.material_path),
            str(self.one_shot_path),
            "--title",
            "Custom Title",
            "--description",
            "Custom Description",
            "--creator",
            "Custom Creator",
        ]
        with redirect_stdout(StringIO()), redirect_stderr(StringIO()):
            main([*arguments, "--output", str(first)])
            main([*arguments, "--output", str(second)])

        value = json.loads(first.read_text())
        inventory = value["resource"]["inventoryData"]
        self.assertEqual(first.read_bytes(), second.read_bytes())
        self.assertEqual(inventory["userCreatedDetails"]["name"], "Custom Title")
        self.assertEqual(
            inventory["userCreatedDetails"]["description"],
            "Custom Description",
        )
        self.assertEqual(inventory["creator"], "Custom Creator")

    def test_lbp_toolkit_export_rejects_digest_mismatch_before_writing(self) -> None:
        placement = json.loads(self.one_shot_path.read_text())
        placement["material_digest"] = "f" * 64
        mismatched = Path(self.directory.name) / "mismatched.json"
        mismatched.write_text(json.dumps(placement))
        output = Path(self.directory.name) / "must-not-exist.json"

        with redirect_stderr(StringIO()), self.assertRaises(SystemExit) as raised:
            main(
                [
                    "export",
                    "lbp-toolkit",
                    str(self.material_path),
                    str(mismatched),
                    "--output",
                    str(output),
                ]
            )

        self.assertEqual(raised.exception.code, 1)
        self.assertFalse(output.exists())

    def test_synthesis_flat_removes_nested_material_occurrences(self) -> None:
        material = Path(self.directory.name) / "flat-nested-material.json"
        with redirect_stdout(StringIO()), redirect_stderr(StringIO()):
            main(
                [
                    "compile",
                    str(NESTED_FIXTURE),
                    "--synthesis-hierarchy",
                    "flat",
                    "--emit-material",
                    str(material),
                ]
            )

        value = json.loads(material.read_text())
        self.assertEqual(len(value["modules"]), 1)

    def test_lbp_toolkit_export_preserves_module_microchips(self) -> None:
        root = Path(self.directory.name) / "hierarchical"
        material = root / "material.json"
        placement = root / "placement.json"
        output = root / "object.json"
        with redirect_stdout(StringIO()), redirect_stderr(StringIO()):
            main(
                [
                    "compile",
                    str(NESTED_FIXTURE),
                    "--emit-material",
                    str(material),
                    "--emit-placement",
                    str(placement),
                    "--physical-hierarchy",
                    "preserve-all",
                ]
            )
            main(
                [
                    "export",
                    "lbp-toolkit",
                    str(material),
                    str(placement),
                    "--output",
                    str(output),
                ]
            )

        value = json.loads(output.read_text())
        things = {}

        def collect(item):
            if isinstance(item, dict):
                if isinstance(item.get("UID"), int):
                    things[item["UID"]] = item
                for child in item.values():
                    collect(child)
            elif isinstance(item, list):
                for child in item:
                    collect(child)

        collect(value)
        chips = [
            item
            for item in things.values()
            if item.get("PSwitch", {}).get("type") == "MICROCHIP"
        ]
        self.assertEqual(len(chips), 3)


if __name__ == "__main__":
    unittest.main()
