import argparse
from dataclasses import replace
import json
import os
from collections.abc import Mapping, Sequence
from pathlib import Path
import sys
import tempfile

from pyosys import libyosys as ys
from gateforge.design import DesignContext, export_json
from gateforge.graph import MaterialGraph
from gateforge.hierarchy import (
    PhysicalHierarchyMode,
    PhysicalHierarchyPolicy,
    SynthesisHierarchyMode,
    SynthesisHierarchyPolicy,
    apply_synthesis_hierarchy,
)
from gateforge.material import MaterialDesign
from gateforge.mapping import Mapper, MappingProvider
from gateforge.materialize import flatten_and_materialize
from gateforge.placement import (
    PlacedDesign,
    TopologicalPlacementOptions,
    TopologicalPlacer,
)
from gateforge.pipeline import MappingStage, default_mapping_stages
from gateforge.provider import TargetProvider
from gateforge.providers.lbp.common import LBP_PROVIDER
from gateforge.providers.lbp.hierarchy import containerize_lbp_plan
from gateforge.providers.lbp.associative import LBPAssociativeConeMapper
from gateforge.providers.lbp.intrinsics import LBPIntrinsicMapper
from gateforge.providers.lbp.mappers import LBPCombinatorialLowLevelGateMapper
from gateforge.providers.lbp.objects import make_lbp_provider
from gateforge.providers.lbp.realize import realize_lbp_plan
from gateforge.providers.lbp.toolkit import encode_lbp_toolkit_plan
from gateforge.search import (
    CompilationSearch,
    MappingSearchMode,
    MappingSearchOptions,
    MappingSearchReport,
    MappingSearchResult,
    TerminalCandidateScore,
)
from gateforge.source import DesignSnapshot
from gateforge.state import CompilationIntermediateState


def design_preprocess(path: str) -> DesignContext:
    context = DesignContext(ys.Design())
    intrinsic_library = Path(__file__).parent / "intrinsics" / "gateforge_intrinsics.v"
    context.run_pass(
        f"read_verilog -lib {json.dumps(str(intrinsic_library.resolve()))}"
    )
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
    synthesis_hierarchy: SynthesisHierarchyPolicy = SynthesisHierarchyPolicy(),
    mapping_search: MappingSearchOptions = MappingSearchOptions(
        mode=MappingSearchMode.GREEDY
    ),
) -> tuple[DesignContext, CompilationIntermediateState]:
    if stages is None:
        stages = default_mapping_stages()
    if mapping_providers is None:
        mapping_providers = (
            LBPIntrinsicMapper(),
            LBPAssociativeConeMapper(),
            LBPCombinatorialLowLevelGateMapper(),
        )
    if target_providers is None:
        target_providers = {LBP_PROVIDER: make_lbp_provider()}

    result = _search_source(
        path,
        stages,
        mapping_providers,
        target_providers,
        synthesis_hierarchy,
        mapping_search,
    )
    winner = result.candidates[0]
    return winner.restore_context(), winner.state


def _search_source(
    path: str,
    stages: Sequence[MappingStage],
    mapping_providers: Sequence[MappingProvider],
    target_providers: Mapping[str, TargetProvider],
    synthesis_hierarchy: SynthesisHierarchyPolicy,
    mapping_search: MappingSearchOptions,
) -> MappingSearchResult:
    context = design_preprocess(path)
    apply_synthesis_hierarchy(context, synthesis_hierarchy)
    state = CompilationIntermediateState.empty(context.revision)
    return CompilationSearch(
        Mapper(mapping_providers),
        target_providers,
        mapping_search,
    ).run(context, state, stages)


def compile_material(
    path: str,
    *,
    stages: Sequence[MappingStage] | None = None,
    mapping_providers: Sequence[MappingProvider] | None = None,
    target_providers: Mapping[str, TargetProvider] | None = None,
    synthesis_hierarchy: SynthesisHierarchyPolicy = SynthesisHierarchyPolicy(),
    mapping_search: MappingSearchOptions = MappingSearchOptions(
        mode=MappingSearchMode.GREEDY
    ),
) -> tuple[DesignContext, CompilationIntermediateState, MaterialDesign]:
    context, state, material, _ = compile_material_with_report(
        path,
        stages=stages,
        mapping_providers=mapping_providers,
        target_providers=target_providers,
        synthesis_hierarchy=synthesis_hierarchy,
        mapping_search=mapping_search,
    )
    return context, state, material


def compile_material_with_report(
    path: str,
    *,
    stages: Sequence[MappingStage] | None = None,
    mapping_providers: Sequence[MappingProvider] | None = None,
    target_providers: Mapping[str, TargetProvider] | None = None,
    synthesis_hierarchy: SynthesisHierarchyPolicy = SynthesisHierarchyPolicy(),
    mapping_search: MappingSearchOptions = MappingSearchOptions(
        mode=MappingSearchMode.GREEDY
    ),
) -> tuple[
    DesignContext,
    CompilationIntermediateState,
    MaterialDesign,
    MappingSearchReport,
]:
    if stages is None:
        stages = default_mapping_stages()
    if mapping_providers is None:
        mapping_providers = (
            LBPIntrinsicMapper(),
            LBPAssociativeConeMapper(),
            LBPCombinatorialLowLevelGateMapper(),
        )
    if target_providers is None:
        target_providers = {LBP_PROVIDER: make_lbp_provider()}
    result = _search_source(
        path,
        stages,
        mapping_providers,
        target_providers,
        synthesis_hierarchy,
        mapping_search,
    )
    evaluated: list[
        tuple[
            tuple[float, int, str, str],
            DesignContext,
            CompilationIntermediateState,
            MaterialDesign,
            TerminalCandidateScore,
        ]
    ] = []
    residual_details: list[str] = []
    for candidate in result.candidates:
        context = candidate.restore_context()
        residual = unmapped_cells(context.snapshot())
        if residual:
            residual_details.extend(
                f"{module}.{name} ({cell_type})"
                for module, name, cell_type in residual
            )
            continue
        state, material = flatten_and_materialize(
            context,
            candidate.state,
            target_providers,
        )
        object_cost = sum(
            target_providers[item.type.provider].material_object_cost(item)
            for item in material.objects
        )
        terminal_score = TerminalCandidateScore(
            candidate=candidate.identifier,
            material_digest=material.get_digest().value,
            provider_object_cost=object_cost,
            object_count=len(material.objects),
            decisions=candidate.decisions,
        )
        evaluated.append(
            (
                (
                    object_cost,
                    len(material.objects),
                    material.get_digest().value,
                    candidate.identifier.value,
                ),
                context,
                state,
                material,
                terminal_score,
            )
        )
    if not evaluated:
        details = ", ".join(sorted(set(residual_details)))
        raise RuntimeError(f"Design contains unmapped cells: {details}")
    _, context, state, material, winner_score = min(
        evaluated,
        key=lambda item: item[0],
    )
    report = replace(
        result.report,
        terminal_scores=tuple(
            sorted(
                (item[4] for item in evaluated),
                key=lambda item: item.candidate.value,
            )
        ),
        winner=winner_score.candidate,
    )
    return context, state, material, report


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
        default=210.0,
        help="Horizontal spacing between topological columns.",
    )
    parser.add_argument(
        "--row-pitch",
        type=float,
        default=105.0,
        help="Vertical spacing between subjects in a column.",
    )
    parser.add_argument(
        "--routing-group-size",
        type=int,
        default=5,
        help="Number of content rows between aligned wire-routing gaps.",
    )
    parser.add_argument(
        "--routing-gap-rows",
        type=int,
        default=1,
        help="Number of empty rows reserved for each wire-routing gap.",
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
            routing_group_size=args.routing_group_size,
            routing_gap_rows=args.routing_gap_rows,
        ),
        providers=providers,
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
    context, state, material, search_report = compile_material_with_report(
        args.source,
        stages=default_mapping_stages(use_abc=not args.no_abc),
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
    plan = realize_lbp_plan(
        material,
        graph,
        placed,
        title=args.title,
        description=args.description,
        creator=args.creator,
    )
    plan = containerize_lbp_plan(
        plan,
        material,
        graph,
        placed,
        PhysicalHierarchyPolicy(
            PhysicalHierarchyMode(args.physical_hierarchy),
            args.hierarchy_threshold,
        ),
        providers,
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
    toolkit_parser.add_argument(
        "--physical-hierarchy",
        choices=tuple(item.value for item in PhysicalHierarchyMode),
        default=PhysicalHierarchyMode.FLAT.value,
        help="Choose which HDL module occurrences become nested microchips.",
    )
    toolkit_parser.add_argument(
        "--hierarchy-threshold",
        type=float,
        help="Threshold used by min-objects or min-cost physical hierarchy.",
    )

    args = parser.parse_args(argv)

    try:
        if args.command == "compile":
            _compile_command(args)
        elif args.command == "place":
            _place_command(args)
        elif args.command == "export" and args.export_format == "lbp-toolkit":
            _export_lbp_toolkit_command(args)
        else:
            raise ValueError(f"Unknown export format {args.export_format!r}")
    except (OSError, RuntimeError, ValueError) as error:
        parser.exit(1, f"gateforge: error: {error}\n")


if __name__ == "__main__":
    main()
