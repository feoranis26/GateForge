import argparse
import json
from collections.abc import Mapping, Sequence
from pathlib import Path
import sys

from gateforge.artifacts import write_json as _write_json
from gateforge.compiler import (
    compile_material,
    compile_material_with_report,
    compile_source,
    default_mapping_providers as _default_mapping_providers,
    default_target_providers as _target_providers,
    design_preprocess,
    search_source as _search_source,
    unmapped_cells,
)
from gateforge.design import export_json
from gateforge.graph import MaterialGraph
from gateforge.hierarchy import (
    GeneratedHierarchyMode,
    GeneratedHierarchyPolicy,
    PhysicalHierarchyMode,
    PhysicalHierarchyPolicy,
    SynthesisHierarchyMode,
    SynthesisHierarchyPolicy,
)
from gateforge.material import MaterialDesign
from gateforge.placement import (
    PlacedDesign,
    TopologicalPlacementOptions,
    TopologicalPlacer,
)
from gateforge.pipeline import MappingStage, default_mapping_stages
from gateforge.provider import TargetProvider
from gateforge.providers.lbp.export import build_lbp_plan
from gateforge.providers.lbp.registers import (
    LBPRegisterStyle,
)
from gateforge.providers.lbp.toolkit import encode_lbp_toolkit_plan
from gateforge.search import (
    MappingSearchMode,
    MappingSearchOptions,
)
from gateforge.visualization.build import build_visual_document


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
        default=210.0,
        help="Horizontal spacing between topological columns.",
    )
    parser.add_argument(
        "--row-pitch",
        type=float,
        default=52.5,
        help="Minimum vertical height reserved for a placed subject.",
    )
    parser.add_argument(
        "--routing-group-height",
        type=float,
        default=250.0,
        help="Content height in world units between wire-routing gaps.",
    )
    parser.add_argument(
        "--routing-gap-rows",
        type=int,
        default=1,
        help="Number of empty rows reserved for each wire-routing gap.",
    )
    parser.add_argument(
        "--physical-hierarchy",
        choices=tuple(item.value for item in PhysicalHierarchyMode),
        default=PhysicalHierarchyMode.FLAT.value,
        help="Choose which HDL module occurrences become nested microchips.",
    )
    parser.add_argument(
        "--hierarchy-threshold",
        type=float,
        help="Threshold used by min-objects or min-cost physical hierarchy.",
    )
    parser.add_argument(
        "--generated-hierarchy",
        choices=tuple(item.value for item in GeneratedHierarchyMode),
        default=GeneratedHierarchyMode.AUTO.value,
        help="Choose inline, automatic, or all generated implementation microchips.",
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
            routing_group_height=args.routing_group_height,
            routing_gap_rows=args.routing_gap_rows,
        ),
        providers=providers,
        physical_hierarchy=PhysicalHierarchyPolicy(
            PhysicalHierarchyMode(args.physical_hierarchy),
            args.hierarchy_threshold,
        ),
        generated_hierarchy=GeneratedHierarchyPolicy(
            GeneratedHierarchyMode(args.generated_hierarchy),
        ),
    )
    return placer.place(graph).finalize(graph)


def _load_json(path: Path) -> object:
    return json.loads(
        path.read_text(encoding="utf-8"),
        parse_constant=lambda token: (_ for _ in ()).throw(
            ValueError(f"Invalid JSON number {token}")
        ),
    )


def _compile_command(args: argparse.Namespace) -> None:
    if args.emit_placement is not None and args.emit_material is None:
        raise ValueError("--emit-placement requires --emit-material")
    providers = _target_providers()
    context, state, material, search_report = compile_material_with_report(
        args.source,
        stages=default_mapping_stages(use_abc=not args.no_abc),
        mapping_providers=_default_mapping_providers(
            LBPRegisterStyle(args.register_style)
        ),
        target_providers=providers,
        synthesis_hierarchy=SynthesisHierarchyPolicy(
            SynthesisHierarchyMode(args.synthesis_hierarchy),
            args.synthesis_threshold,
        ),
        mapping_search=MappingSearchOptions(
            mode=MappingSearchMode(args.mapping_search),
            beam_width=args.mapping_beam_width,
            stage_alternative_limit=args.mapping_stage_limit,
        ),
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
    if args.emit_search_report is not None:
        _write_json(args.emit_search_report, search_report.canonical_data())
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
    virtual_terminals = len(placed.subjects) - len(material.objects)
    if args.output is not None:
        _write_json(args.output, placed.canonical_data())
        print(
            f"Placed {len(material.objects)} objects and "
            f"{virtual_terminals} "
            "virtual terminals."
        )
        return
    print(json.dumps(placed.canonical_data(), indent=4, allow_nan=False))
    print(
        f"Placed {len(material.objects)} objects and "
        f"{virtual_terminals} "
        "virtual terminals.",
        file=sys.stderr,
    )


def _export_lbp_toolkit_command(args: argparse.Namespace) -> None:
    providers = _target_providers()
    material = MaterialDesign.from_canonical_data(
        _load_json(args.material),
        providers,
    )
    graph = MaterialGraph.from_design(material, providers)
    placed = PlacedDesign.from_canonical_data(
        _load_json(args.placement),
        graph,
    )
    plan = build_lbp_plan(
        material,
        graph,
        placed,
        providers,
        title=args.title,
        description=args.description,
        creator=args.creator,
    )
    _write_json(args.output, encode_lbp_toolkit_plan(plan))
    material_gadgets = sum(
        item.source.value == "material_object" for item in plan.gadgets
    )
    io_gadgets = sum(item.source.value == "module_port" for item in plan.gadgets)
    batteries = sum(item.source.value == "constant" for item in plan.gadgets)
    print(
        f"Exported {material_gadgets} material gadgets, {io_gadgets} I/O buffers, "
        f"{batteries} batteries, {len(plan.notes)} notes, and "
        f"{len(plan.connections)} connections on a "
        f"{plan.board_size.x:g} x {plan.board_size.y:g} circuit board."
    )


def _visualize_command(args: argparse.Namespace) -> None:
    def load_document():
        providers = _target_providers()
        material = MaterialDesign.from_canonical_data(
            _load_json(args.material),
            providers,
        )
        graph = MaterialGraph.from_design(material, providers)
        placed = PlacedDesign.from_canonical_data(
            _load_json(args.placement),
            graph,
        )
        return build_visual_document(
            material,
            graph,
            placed,
            providers,
        )

    _launch_visualizer(
        load_document(),
        reload_document=load_document,
        watch_paths=(args.material, args.placement),
        watch=args.watch,
    )


def _launch_visualizer(document, **options) -> None:
    try:
        from gateforge.visualization.tk_app import launch_visualizer
    except ModuleNotFoundError as error:
        if error.name != "tkinter":
            raise
        raise RuntimeError(
            "Tkinter is required for visualization; install the python3-tk package"
        ) from error

    launch_visualizer(document, **options)


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
        "--emit-search-report",
        type=Path,
        help="Write mapping decisions, candidate counts, and terminal scores as JSON.",
    )
    compile_parser.add_argument(
        "--no-abc",
        action="store_true",
        help="Skip ABC optimization before the final leaf mapping stage.",
    )
    compile_parser.add_argument(
        "--mapping-search",
        choices=tuple(item.value for item in MappingSearchMode),
        default=MappingSearchMode.GREEDY.value,
        help="Choose greedy compatibility, beam, or exhaustive mapping search.",
    )
    compile_parser.add_argument(
        "--mapping-beam-width",
        type=int,
        default=16,
        help="Maximum candidates retained after each beam-search stage.",
    )
    compile_parser.add_argument(
        "--mapping-stage-limit",
        type=int,
        default=16,
        help="Maximum proposal sets generated per candidate and stage.",
    )
    compile_parser.add_argument(
        "--register-style",
        choices=tuple(item.value for item in LBPRegisterStyle),
        default=LBPRegisterStyle.COMPACT.value,
        help="Choose compact Counter-based or hardened two-phase register banks.",
    )
    compile_parser.add_argument(
        "--show",
        action="store_true",
        help="Open the final Yosys graph visualization.",
    )
    compile_parser.add_argument(
        "--synthesis-hierarchy",
        choices=tuple(item.value for item in SynthesisHierarchyMode),
        default=SynthesisHierarchyMode.PRESERVE.value,
        help="Choose module flattening before mapping and optimization.",
    )
    compile_parser.add_argument(
        "--synthesis-threshold",
        type=int,
        help="Primitive-cell threshold used by --synthesis-hierarchy=min-cells.",
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

    export_parser = subparsers.add_parser(
        "export",
        help="Export paired material and placement artifacts.",
    )
    export_subparsers = export_parser.add_subparsers(
        dest="export_format",
        required=True,
    )
    toolkit_parser = export_subparsers.add_parser(
        "lbp-toolkit",
        help="Write Craftworld Toolkit-compatible LBP PLAN JSON.",
    )
    toolkit_parser.add_argument("material", type=Path)
    toolkit_parser.add_argument("placement", type=Path)
    toolkit_parser.add_argument("--output", type=Path, required=True)
    toolkit_parser.add_argument(
        "--title",
        help="Override the saved object's inventory title.",
    )
    toolkit_parser.add_argument(
        "--description",
        help="Override the saved object's inventory description.",
    )
    toolkit_parser.add_argument(
        "--creator",
        help="Override creator and creation-history metadata.",
    )
    visualize_parser = subparsers.add_parser(
        "visualize",
        help="Inspect material placement and provider-realized scenes.",
    )
    visualize_parser.add_argument("material", type=Path)
    visualize_parser.add_argument("placement", type=Path)
    visualize_parser.add_argument(
        "--watch",
        action="store_true",
        help="Reload automatically when either input artifact changes.",
    )

    args = parser.parse_args(argv)

    try:
        if args.command == "compile":
            _compile_command(args)
        elif args.command == "place":
            _place_command(args)
        elif args.command == "export" and args.export_format == "lbp-toolkit":
            _export_lbp_toolkit_command(args)
        elif args.command == "visualize":
            _visualize_command(args)
        else:
            raise ValueError(f"Unknown export format {args.export_format!r}")
    except (OSError, RuntimeError, ValueError) as error:
        parser.exit(1, f"gateforge: error: {error}\n")


if __name__ == "__main__":
    main()
