from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from gateforge.gateforge import main


FIXTURE = Path(__file__).parents[1] / "scratch" / "basic.v"


class CliTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.directory = TemporaryDirectory(prefix="gateforge-cli-test-")
        root = Path(cls.directory.name)
        cls.material_path = root / "nested" / "material.json"
        cls.one_shot_path = root / "nested" / "one-shot-placement.json"
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

    @classmethod
    def tearDownClass(cls) -> None:
        cls.directory.cleanup()

    def test_compile_creates_parent_directories_and_both_artifacts(self) -> None:
        self.assertTrue(self.material_path.is_file())
        self.assertTrue(self.one_shot_path.is_file())

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
        self.assertIn("7 material objects", details["description"])
        self.assertIn("Exported 7 material gadgets", stdout.getvalue())

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


if __name__ == "__main__":
    unittest.main()
