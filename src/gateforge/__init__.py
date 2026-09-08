from __future__ import annotations

from collections.abc import Sequence
import sys


def main(argv: Sequence[str] | None = None) -> None:
    arguments = list(sys.argv[1:] if argv is None else argv)
    if arguments[:1] == ["workbench"]:
        try:
            from gateforge.workbench.qt_app import main as run_workbench
        except ModuleNotFoundError as error:
            if error.name is not None and error.name.startswith("PySide6"):
                raise SystemExit(
                    "gateforge: error: the workbench requires PySide6; "
                    "install GateForge with the 'workbench' extra"
                ) from error
            raise
        raise SystemExit(run_workbench(arguments[1:]))

    from gateforge.gateforge import main as run

    run(arguments)
