from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path
import subprocess
import sys
import unittest
from unittest.mock import patch

try:
    import PySide6  # noqa: F401
except ModuleNotFoundError as error:
    raise unittest.SkipTest("PySide6 workbench extra is not installed") from error

import gateforge
from gateforge.workbench import qt_app


class WorkbenchEntrypointTests(unittest.TestCase):
    def test_dispatches_workbench_arguments_to_qt_entrypoint(self) -> None:
        with patch("gateforge.workbench.qt_app.main", return_value=0) as run:
            with self.assertRaises(SystemExit) as raised:
                gateforge.main(["workbench", "example.v"])

        self.assertEqual(raised.exception.code, 0)
        run.assert_called_once_with(["example.v"])

    def test_dispatches_factorio_target_to_qt_entrypoint(self) -> None:
        with patch("gateforge.workbench.qt_app.main", return_value=0) as run:
            with self.assertRaises(SystemExit) as raised:
                gateforge.main(
                    ["workbench", "--target", "factorio", "example.v"]
                )

        self.assertEqual(raised.exception.code, 0)
        run.assert_called_once_with(["--target", "factorio", "example.v"])

    def test_workbench_help_does_not_import_pyosys(self) -> None:
        result = subprocess.run(
            [
                sys.executable,
                "-c",
                (
                    "import sys; import gateforge; "
                    "\ntry: gateforge.main(['workbench', '--help'])"
                    "\nexcept SystemExit as error: "
                    "assert error.code == 0; assert 'pyosys' not in sys.modules"
                ),
            ],
            check=False,
            capture_output=True,
            text=True,
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("GateForge compiler workbench", result.stdout)

    def test_qt_entrypoint_accepts_factorio_target(self) -> None:
        with patch.object(qt_app, "launch_workbench", return_value=0) as launch:
            result = qt_app.main(["--target", "factorio", "example.v"])

        self.assertEqual(result, 0)
        launch.assert_called_once_with(Path("example.v"), target="factorio")


if __name__ == "__main__":
    unittest.main()