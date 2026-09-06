import argparse
import json
import os
from collections.abc import Mapping, Sequence
from pathlib import Path
import sys
import tempfile

from pyosys import libyosys as ys
from gateforge.design import DesignContext, export_json
from gateforge.graph import MaterialGraph
from gateforge.material import MaterialDesign
from gateforge.mapping import Mapper, MappingProvider
from gateforge.materialize import flatten_and_materialize
from gateforge.placement import (
    PlacedDesign,
    TopologicalPlacementOptions,
    TopologicalPlacer,
)
from gateforge.pipeline import MappingStage, default_mapping_stages, run_mapping_stages
from gateforge.provider import TargetProvider
from gateforge.providers.lbp.common import LBP_PROVIDER
from gateforge.providers.lbp.mappers import LBPCombinatorialLowLevelGateMapper
from gateforge.providers.lbp.objects import make_lbp_provider
from gateforge.source import DesignSnapshot
from gateforge.state import CompilationIntermediateState


def design_preprocess(path: str) -> DesignContext:
    context = DesignContext(ys.Design())
    context.run_pass(f"read_verilog {json.dumps(os.path.abspath(path))}")
    context.run_pass("hierarchy -check -auto-top")
    context.run_pass("proc")
    return context


def unmapped_cells(design: DesignSnapshot) -> tuple[tuple[str, str, str], ...]:
    residual: list[tuple[str, str, str]] = []
    module_names = set(design.modules)
    for module in design.modules.values():
        if "blackbox" in module.attributes:
            continue
        for cell in module.cells.values():
            if "gateforge_prefab" in cell.attributes:
                continue
            if cell.identifier.expected_type in module_names:
                continue
            if cell.identifier.expected_type == "$scopeinfo":
                continue
            residual.append(
                (
                    module.name,
                    cell.identifier.name,
                    cell.identifier.expected_type,
                )
            )
    return tuple(sorted(residual))


def compile_source(
    path: str,
    *,
    stages: Sequence[MappingStage] | None = None,
    mapping_providers: Sequence[MappingProvider] | None = None,
    target_providers: Mapping[str, TargetProvider] | None = None,
) -> tuple[DesignContext, CompilationIntermediateState]:
    context = design_preprocess(path)
    state = CompilationIntermediateState.empty(context.revision)
    if stages is None:
        stages = default_mapping_stages()
    if mapping_providers is None:
        mapping_providers = (LBPCombinatorialLowLevelGateMapper(),)
    if target_providers is None:
        target_providers = {LBP_PROVIDER: make_lbp_provider()}

    state = run_mapping_stages(
        context,
        state,
        stages,
        Mapper(mapping_providers),
        target_providers,
    )
    return context, state


def compile_material(
    path: str,
    *,
    stages: Sequence[MappingStage] | None = None,
    mapping_providers: Sequence[MappingProvider] | None = None,
    target_providers: Mapping[str, TargetProvider] | None = None,
) -> tuple[DesignContext, CompilationIntermediateState, MaterialDesign]:
    if target_providers is None:
        target_providers = {LBP_PROVIDER: make_lbp_provider()}
    context, state = compile_source(
        path,
        stages=stages,
        mapping_providers=mapping_providers,
        target_providers=target_providers,
    )
    residual = unmapped_cells(context.snapshot())
    if residual:
        details = ", ".join(
            f"{module}.{name} ({cell_type})"
            for module, name, cell_type in residual
        )
        raise RuntimeError(f"Design contains unmapped cells: {details}")
    state, material = flatten_and_materialize(
        context,
        state,
        target_providers,
    )

    return context, state, material


def _target_providers() -> dict[str, TargetProvider]:
    return {LBP_PROVIDER: make_lbp_provider()}


def _add_placement_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--placer",
        choices=("topological",),
        default="topological",
        help="Select the placement strategy.",
    )
    parser.add_argument(
        "--column-pitch",
        type=float,
        default=262.5,
        help="Horizontal spacing between topological columns.",
    )
    parser.add_argument(
        "--row-pitch",
        type=float,
        default=262.5,
        help="Vertical spacing between subjects in a column.",
    )


def _place(
    material: MaterialDesign,
    providers: Mapping[str, TargetProvider],
    args: argparse.Namespace,
) -> PlacedDesign:
    graph = MaterialGraph.from_design(material, providers)
    if args.placer != "topological":
        raise ValueError(f"Unknown placer {args.placer!r}")
    placer = TopologicalPlacer(
        TopologicalPlacementOptions(
            column_pitch=args.column_pitch,
            row_pitch=args.row_pitch,
        )
    )
    return placer.place(graph).finalize(graph)


def _load_json(path: Path) -> object:
    return json.loads(
        path.read_text(encoding="utf-8"),
        parse_constant=lambda token: (_ for _ in ()).throw(
            ValueError(f"Invalid JSON number {token}")
        ),
    )


def _write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        dir=path.parent,
        prefix=f".{path.name}.",
        suffix=".tmp",
        text=True,
    )
    temporary_path = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            json.dump(value, stream, indent=4, allow_nan=False)
            stream.write("\n")
        os.replace(temporary_path, path)
    finally:
        temporary_path.unlink(missing_ok=True)


def _compile_command(args: argparse.Namespace) -> None:
    if args.emit_placement is not None and args.emit_material is None:
        raise ValueError("--emit-placement requires --emit-material")
    providers = _target_providers()
    context, state, material = compile_material(
        args.source,
        stages=default_mapping_stages(use_abc=not args.no_abc),
        target_providers=providers,
    )
    placed = (
        _place(material, providers, args)
        if args.emit_placement is not None
        else None
    )
    if args.show:
        context.show()
    if args.emit_json is not None:
        _write_json(args.emit_json, export_json(context.design))
    if args.emit_material is not None:
        _write_json(args.emit_material, material.canonical_data())
    if args.emit_state is not None:
        _write_json(args.emit_state, state.canonical_data())
    if args.emit_placement is not None and placed is not None:
        _write_json(args.emit_placement, placed.canonical_data())
    print(
        f"Accepted {len(state.claims)} claims using "
        f"{len(state.prefabs)} semantic prefabs."
    )
    print(
        f"Materialized {len(material.objects)} objects and "
        f"{len(material.nets)} networks."
    )


def _place_command(args: argparse.Namespace) -> None:
    providers = _target_providers()
    material = MaterialDesign.from_canonical_data(
        _load_json(args.material),
        providers,
    )
    placed = _place(material, providers, args)
    if args.output is not None:
        _write_json(args.output, placed.canonical_data())
        print(
            f"Placed {len(placed.placements.objects)} objects and "
            f"{len(placed.placements.module_ports) + len(placed.placements.constants)} "
            "virtual terminals."
        )
        return
    print(json.dumps(placed.canonical_data(), indent=4, allow_nan=False))
    print(
        f"Placed {len(placed.placements.objects)} objects and "
        f"{len(placed.placements.module_ports) + len(placed.placements.constants)} "
        "virtual terminals.",
        file=sys.stderr,
    )


def main(argv: Sequence[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        description="Compile synthesizable Verilog for a GateForge target."
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    compile_parser = subparsers.add_parser(
        "compile",
        help="Compile Verilog into a GateForge material design.",
    )
    compile_parser.add_argument("source")
    compile_parser.add_argument(
        "--emit-json",
        type=Path,
        help="Write the final flattened RTLIL design as Yosys JSON.",
    )
    compile_parser.add_argument(
        "--emit-material",
        type=Path,
        help="Write the material object/network graph as JSON.",
    )
    compile_parser.add_argument(
        "--emit-placement",
        type=Path,
        help="Place the material design and write placement JSON.",
    )
    compile_parser.add_argument(
        "--emit-state",
        type=Path,
        help="Write semantic prefabs and durable claim definitions as JSON.",
    )
    compile_parser.add_argument(
        "--no-abc",
        action="store_true",
        help="Skip ABC optimization before the final leaf mapping stage.",
    )
    compile_parser.add_argument(
        "--show",
        action="store_true",
        help="Open the final Yosys graph visualization.",
    )
    _add_placement_arguments(compile_parser)

    place_parser = subparsers.add_parser(
        "place",
        help="Place an existing GateForge material design.",
    )
    place_parser.add_argument("material", type=Path)
    place_parser.add_argument(
        "--output",
        type=Path,
        help="Write placement JSON instead of emitting it to stdout.",
    )
    _add_placement_arguments(place_parser)

    args = parser.parse_args(argv)

    try:
        if args.command == "compile":
            _compile_command(args)
        else:
            _place_command(args)
    except (OSError, RuntimeError, ValueError) as error:
        parser.exit(1, f"gateforge: error: {error}\n")


if __name__ == "__main__":
    main()
