from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
import hashlib
import json
from pathlib import Path
import shutil
import subprocess
from tempfile import TemporaryDirectory

from pyosys import libyosys as ys

from gateforge.design import DesignCheckpoint, rtlil_id
from gateforge.source import ModuleSnapshot


class YosysSchematicError(RuntimeError):
    code = "schematic-failed"


class GraphvizUnavailableError(YosysSchematicError):
    code = "graphviz-unavailable"


class UnknownSchematicModuleError(YosysSchematicError):
    code = "unknown-module"


@dataclass(frozen=True, slots=True)
class SchematicRenderOptions:
    show_widths: bool = False
    stretch_ports: bool = False

    def __post_init__(self) -> None:
        if type(self.show_widths) is not bool:
            raise TypeError("show_widths must be a boolean")
        if type(self.stretch_ports) is not bool:
            raise TypeError("stretch_ports must be a boolean")

    def canonical_data(self) -> dict[str, bool]:
        return {
            "show_widths": self.show_widths,
            "stretch_ports": self.stretch_ports,
        }


@dataclass(frozen=True, slots=True)
class YosysSchematic:
    cache_key: str
    checkpoint_digest: str
    revision: int
    module: str
    modules: tuple[str, ...]
    options: SchematicRenderOptions
    dot: str
    svg: str

    def canonical_data(self) -> dict[str, object]:
        return {
            "schema_version": 1,
            "cache_key": self.cache_key,
            "checkpoint_digest": self.checkpoint_digest,
            "revision": self.revision,
            "module": self.module,
            "modules": list(self.modules),
            "options": self.options.canonical_data(),
            "dot": self.dot,
            "svg": self.svg,
        }


class YosysSchematicRenderer:
    def __init__(self, *, graphviz_timeout: float = 30.0) -> None:
        if graphviz_timeout <= 0:
            raise ValueError("graphviz_timeout must be positive")
        self.graphviz_timeout = graphviz_timeout
        self._cache: dict[str, YosysSchematic] = {}

    @property
    def cache_size(self) -> int:
        return len(self._cache)

    def clear(self) -> None:
        self._cache.clear()

    def render(
        self,
        checkpoint: DesignCheckpoint,
        *,
        module: str | None = None,
        options: SchematicRenderOptions = SchematicRenderOptions(),
    ) -> YosysSchematic:
        checkpoint_digest = checkpoint.get_digest()
        request_key = _cache_key(checkpoint_digest, module, options)
        cached = self._cache.get(request_key)
        if cached is not None:
            return cached

        context = checkpoint.restore()
        snapshot = context.snapshot()
        modules = tuple(sorted(snapshot.modules))
        selected_module = _select_module(snapshot.modules, module)
        dot_executable = shutil.which("dot")
        if dot_executable is None:
            raise GraphvizUnavailableError(
                "Graphviz 'dot' is unavailable; install Graphviz to view "
                "Yosys schematics"
            )

        with TemporaryDirectory(prefix="gateforge-schematic-") as directory:
            prefix = Path(directory) / "schematic"
            flags = ["show", "-format", "dot"]
            if options.show_widths:
                flags.append("-width")
            if options.stretch_ports:
                flags.append("-stretch")
            flags.extend(("-prefix", str(prefix), selected_module))
            context.design.run_pass(ys.StringVector(flags))
            dot_path = prefix.with_suffix(".dot")
            if not dot_path.is_file():
                raise YosysSchematicError(
                    f"Yosys did not produce a DOT schematic for {selected_module!r}"
                )
            dot = dot_path.read_text(encoding="utf-8")

        try:
            rendered = subprocess.run(
                [dot_executable, "-Tsvg"],
                input=dot,
                check=False,
                capture_output=True,
                text=True,
                timeout=self.graphviz_timeout,
            )
        except subprocess.TimeoutExpired as error:
            raise YosysSchematicError(
                f"Graphviz exceeded the {self.graphviz_timeout:g}-second timeout"
            ) from error
        if rendered.returncode != 0:
            detail = rendered.stderr.strip() or "unknown Graphviz error"
            raise YosysSchematicError(f"Graphviz failed: {detail}")
        if not rendered.stdout.lstrip().startswith("<?xml"):
            raise YosysSchematicError("Graphviz did not produce an SVG document")

        schematic = YosysSchematic(
            cache_key=request_key,
            checkpoint_digest=checkpoint_digest,
            revision=checkpoint.revision,
            module=selected_module,
            modules=modules,
            options=options,
            dot=dot,
            svg=rendered.stdout,
        )
        self._cache[request_key] = schematic
        return schematic


def _select_module(
    modules: Mapping[str, ModuleSnapshot],
    requested: str | None,
) -> str:
    if requested is not None:
        if not requested:
            raise UnknownSchematicModuleError("Schematic module must not be empty")
        if requested not in modules:
            raise UnknownSchematicModuleError(
                f"Design checkpoint has no module {requested!r}"
            )
        return requested
    top_modules = sorted(
        name
        for name, item in modules.items()
        if "top" in item.attributes and "blackbox" not in item.attributes
    )
    if top_modules:
        return top_modules[0]
    concrete_modules = sorted(
        name for name, item in modules.items() if "blackbox" not in item.attributes
    )
    if concrete_modules:
        return concrete_modules[0]
    if modules:
        return sorted(modules)[0]
    raise UnknownSchematicModuleError("Design checkpoint contains no modules")


def _cache_key(
    checkpoint_digest: str,
    module: str | None,
    options: SchematicRenderOptions,
) -> str:
    encoded = json.dumps(
        {
            "checkpoint_digest": checkpoint_digest,
            "module": module,
            "options": options.canonical_data(),
        },
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()