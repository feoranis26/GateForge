import argparse
import json
from collections.abc import Mapping, Sequence
from pathlib import Path
import sys
from typing import TypedDict

from gateforge.artifacts import write_json as _write_json
from gateforge.compiler import (
    all_target_providers as _all_target_providers,
    compilation_backend as _compilation_backend,
    compile_material,
    compile_material_with_report,
    compile_source,
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
from gateforge.providers.factorio.blueprint import build_factorio_blueprint
from gateforge.providers.factorio.routing import build_factorio_routed_design
from gateforge.search import (
    MappingSearchMode,
    MappingSearchOptions,
)
from gateforge.visualization.build import build_visual_document


class _FactorioRealizationOptions(TypedDict):
    input_drivers: str
    input_values: dict[str, str]
    output_lamps: bool


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
        help="Horizontal spacing between topological columns (target default if omitted).",
    )
    parser.add_argument(
        "--row-pitch",
        type=float,
        help="Minimum vertical height per subject (target default if omitted).",
    )
    parser.add_argument(
        "--routing-group-height",
        type=float,
        help="Content height between routing gaps (target default if omitted).",
    )
    parser.add_argument(
        "--routing-gap-rows",
        type=int,
        help="Empty rows per routing gap (target default if omitted).",
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


def _add_factorio_input_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--add-input-combinators",
        action="store_true",
        help="Add a constant combinator for every top-level Factorio input.",
    )
    parser.add_argument(
        "--input-value",
        action="append",
        default=[],
        metavar="PORT=VALUE",
        help="Set a generated input combinator value; repeat for multiple ports.",
    )
    parser.add_argument(
        "--add-output-lamps",
        action="store_true",
        help="Add a circuit-controlled lamp for every top-level Factorio output.",
    )


def _place(
    material: MaterialDesign,
    providers: Mapping[str, TargetProvider],
    args: argparse.Namespace,
) -> PlacedDesign:
    graph = MaterialGraph.from_design(material, providers)
    target = _material_target(material)
    backend = _compilation_backend(target)
    if args.placer != "topological":
        raise ValueError(f"Unknown placer {args.placer!r}")
    defaults = backend.placement_options
    placer = TopologicalPlacer(
        TopologicalPlacementOptions(
            column_pitch=(
                defaults.column_pitch
                if args.column_pitch is None
                else args.column_pitch
            ),
            row_pitch=(
                defaults.row_pitch if args.row_pitch is None else args.row_pitch
            ),
            routing_group_height=(
                defaults.routing_group_height
                if args.routing_group_height is None
                else args.routing_group_height
            ),
            routing_gap_rows=(
                defaults.routing_gap_rows
                if args.routing_gap_rows is None
                else args.routing_gap_rows
            ),
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


def _material_target(material: MaterialDesign) -> str:
    targets = {
        *(item.type.provider for item in material.objects),
        *(item.type.provider for item in material.nets),
    }
    if len(targets) != 1:
        raise ValueError("Material design must contain exactly one target provider")
    return next(iter(targets))


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
    backend = _compilation_backend(
        args.target,
        LBPRegisterStyle(args.register_style),
    )
    providers = dict(backend.target_providers)
    context, state, material, search_report = compile_material_with_report(
        args.source,
        stages=default_mapping_stages(use_abc=not args.no_abc),
        mapping_providers=backend.mapping_providers,
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
    providers = _all_target_providers()
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
    providers = _all_target_providers()
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


def _export_factorio_blueprint_command(args: argparse.Namespace) -> None:
    providers = _all_target_providers()
    material = MaterialDesign.from_canonical_data(
        _load_json(args.material),
        providers,
    )
    target = _material_target(material)
    if target != "factorio":
        raise ValueError("factorio-blueprint export requires a Factorio design")
    graph = MaterialGraph.from_design(material, providers)
    placed = PlacedDesign.from_canonical_data(
        _load_json(args.placement),
        graph,
    )
    routed = build_factorio_routed_design(
        material,
        graph,
        placed,
        **_factorio_realization_options(args, target),
    )
    blueprint = build_factorio_blueprint(routed, label=args.label)
    _write_json(args.output, blueprint.canonical_data())
    print(
        f"Exported Factorio blueprint with {len(routed.entities)} entities, "
        f"{len(routed.wires)} circuit wires, and "
        f"{len(routed.power_segments)} copper wires."
    )


def _visualize_command(args: argparse.Namespace) -> None:
    def load_document():
        providers = _all_target_providers()
        material = MaterialDesign.from_canonical_data(
            _load_json(args.material),
            providers,
        )
        graph = MaterialGraph.from_design(material, providers)
        placed = PlacedDesign.from_canonical_data(
            _load_json(args.placement),
            graph,
        )
        target = _material_target(material)
        return build_visual_document(
            material,
            graph,
            placed,
            providers,
            provider_options={
                "factorio": _factorio_realization_options(args, target)
            },
        )

    _launch_visualizer(
        load_document(),
        reload_document=load_document,
        watch_paths=(args.material, args.placement),
        watch=args.watch,
    )


def _parse_input_values(values: Sequence[str]) -> dict[str, str]:
    result = {}
    for value in values:
        port, separator, raw_value = value.partition("=")
        if not separator or not port or not raw_value:
            raise ValueError(
                f"Invalid input value {value!r}; expected PORT=VALUE"
            )
        if port in result:
            raise ValueError(f"Duplicate input value for port {port!r}")
        result[port] = raw_value
    return result


def _factorio_realization_options(
    args: argparse.Namespace,
    target: str,
) -> _FactorioRealizationOptions:
    input_values = _parse_input_values(args.input_value)
    requested = (
        args.add_input_combinators
        or args.add_output_lamps
        or bool(input_values)
    )
    if requested and target != "factorio":
        raise ValueError(
            "Factorio realization options require a Factorio design"
        )
    if input_values and not args.add_input_combinators:
        raise ValueError("--input-value requires --add-input-combinators")
    return {
        "input_drivers": (
            "constant" if args.add_input_combinators else "none"
        ),
        "input_values": input_values,
        "output_lamps": args.add_output_lamps,
    }


def _launch_visualizer(document, **options) -> None:
    try:
        from gateforge.visualization.qt_app import launch_visualizer
    except ModuleNotFoundError as error:
        if error.name != "PySide6" and not (
            isinstance(error.name, str) and error.name.startswith("PySide6.")
        ):
            raise
        raise RuntimeError(
            "PySide6 is required for visualization; install GateForge with the "
            "workbench extra"
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
        "--target",
        choices=("lbp", "factorio"),
        default="lbp",
        help="Select the compilation target.",
    )
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
    factorio_parser = export_subparsers.add_parser(
        "factorio-blueprint",
        help="Write Factorio 2.0 blueprint JSON.",
    )
    factorio_parser.add_argument("material", type=Path)
    factorio_parser.add_argument("placement", type=Path)
    factorio_parser.add_argument("--output", type=Path, required=True)
    factorio_parser.add_argument(
        "--label",
        help="Set the Factorio blueprint label.",
    )
    _add_factorio_input_arguments(factorio_parser)
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
    _add_factorio_input_arguments(visualize_parser)

    args = parser.parse_args(argv)

    try:
        if args.command == "compile":
            _compile_command(args)
        elif args.command == "place":
            _place_command(args)
        elif args.command == "export" and args.export_format == "lbp-toolkit":
            _export_lbp_toolkit_command(args)
        elif args.command == "export" and args.export_format == "factorio-blueprint":
            _export_factorio_blueprint_command(args)
        elif args.command == "visualize":
            _visualize_command(args)
        else:
            raise ValueError(f"Unknown export format {args.export_format!r}")
    except (OSError, RuntimeError, ValueError) as error:
        parser.exit(1, f"gateforge: error: {error}\n")


if __name__ == "__main__":
    main()
