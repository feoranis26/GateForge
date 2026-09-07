from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any

from pyosys import libyosys as ys

from gateforge.source import (
    CellPortIdentifier,
    ConstantBoundarySource,
    DesignSnapshot,
    SnapshotBitRef,
    SourceCut,
)


class DesignError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class DesignCheckpoint:
    rtlil: str
    revision: int
    scratchpad: tuple[tuple[str, str], ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.revision, int) or isinstance(self.revision, bool):
            raise TypeError("Design checkpoint revision must be an integer")
        if self.revision < 0:
            raise ValueError("Design checkpoint revision must be nonnegative")
        if tuple(sorted(self.scratchpad)) != self.scratchpad:
            raise ValueError("Design checkpoint scratchpad must be sorted")
        if len(dict(self.scratchpad)) != len(self.scratchpad):
            raise ValueError("Design checkpoint scratchpad contains duplicate keys")

    def canonical_bytes(self) -> bytes:
        return json.dumps(
            {
                "revision": self.revision,
                "rtlil": self.rtlil,
                "scratchpad": [list(item) for item in self.scratchpad],
            },
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")

    def get_digest(self) -> str:
        return hashlib.sha256(self.canonical_bytes()).hexdigest()

    def restore(self) -> DesignContext:
        with TemporaryDirectory(prefix="gateforge-checkpoint-") as directory:
            input_path = Path(directory) / "design.il"
            input_path.write_text(self.rtlil, encoding="utf-8")
            design = ys.Design()
            ys.run_pass(f"read_rtlil {json.dumps(str(input_path))}", design)
        for key, value in self.scratchpad:
            design.scratchpad_set_string(key, value)
        if not design.full_selection():
            design.push_full_selection()
        design.check()
        return DesignContext(design, revision=self.revision)


def rtlil_id(name: str) -> ys.IdString:
    if name.startswith(("$", "\\")):
        return ys.IdString(name)
    return ys.IdString(f"\\{name}")


def export_json(design: ys.Design) -> dict[str, Any]:
    with TemporaryDirectory(prefix="gateforge-") as directory:
        output_path = Path(directory) / "design.json"
        ys.run_pass(f"write_json {output_path}", design)
        return json.loads(output_path.read_text())


class DesignContext:
    def __init__(self, design: ys.Design, revision: int = 0):
        self.design = design
        self.revision = revision
        self._snapshot: DesignSnapshot | None = None

    def snapshot(self) -> DesignSnapshot:
        if self._snapshot is None:
            self._snapshot = DesignSnapshot.from_json(
                export_json(self.design),
                revision=self.revision,
            )
        return self._snapshot

    def checkpoint(self) -> DesignCheckpoint:
        self.design.check()
        self.design.sort()
        return DesignCheckpoint(
            rtlil=self.design.to_rtlil_str(only_selected=False),
            revision=self.revision,
            scratchpad=tuple(sorted(self.design.scratchpad.items())),
        )

    def fork(self) -> DesignContext:
        return self.checkpoint().restore()

    def run_pass(self, command: str) -> None:
        ys.run_pass(command, self.design)
        self.revision += 1
        self._snapshot = None

    def mark_mutated(self) -> None:
        self.revision += 1
        self._snapshot = None

    def save_json(self, output_path: Path) -> None:
        output_path.write_text(json.dumps(export_json(self.design), indent=2))

    def show(self) -> None:
        ys.run_pass("show", self.design)

    def module(self, name: str) -> ys.Module:
        module = self.design.module(rtlil_id(name))
        if module is None:
            raise DesignError(f"Module {name!r} no longer exists")
        return module

    def resolve_cell_port(self, endpoint: CellPortIdentifier) -> ys.SigSpec:
        module = self.module(endpoint.cell.module)
        cell = module.cell(rtlil_id(endpoint.cell.name))
        if cell is None:
            raise DesignError(
                f"Cell {endpoint.cell.module}.{endpoint.cell.name} no longer exists"
            )
        if cell.type.unescape() != endpoint.cell.expected_type:
            raise DesignError(
                f"Cell {endpoint.cell.module}.{endpoint.cell.name} changed type from "
                f"{endpoint.cell.expected_type!r} to {cell.type.unescape()!r}"
            )
        port_id = rtlil_id(endpoint.name)
        if not cell.hasPort(port_id):
            raise DesignError(
                f"Cell {endpoint.cell.module}.{endpoint.cell.name} has no port "
                f"{endpoint.name!r}"
            )
        signal = cell.getPort(port_id)
        if endpoint.bit < 0 or endpoint.bit >= signal.size():
            raise DesignError(
                f"Bit {endpoint.bit} is outside {endpoint.cell.module}."
                f"{endpoint.cell.name}.{endpoint.name}[{signal.size() - 1}:0]"
            )
        return ys.SigSpec(signal[endpoint.bit], 1)

    def resolve_cut_source(
        self,
        snapshot: DesignSnapshot,
        cut: SourceCut,
    ) -> ys.SigSpec:
        if snapshot.revision != self.revision:
            raise DesignError(
                f"Snapshot revision {snapshot.revision} does not match live "
                f"revision {self.revision}"
            )
        if isinstance(cut.source, ConstantBoundarySource):
            return self.resolve_cell_port(cut.source.consumer)
        if not isinstance(cut.source, SnapshotBitRef):
            raise DesignError(f"Unsupported cut source {cut.source!r}")
        if cut.source.revision != snapshot.revision:
            raise DesignError(f"Stale snapshot bit {cut.source}")
        if not cut.inside:
            raise DesignError(f"Cut bit {cut.source} has no endpoint inside the region")
        return self.resolve_cell_port(sorted(cut.inside)[0])