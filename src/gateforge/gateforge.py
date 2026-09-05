import argparse
import json
import os
from collections.abc import Mapping, Sequence
from pathlib import Path

from pyosys import libyosys as ys
from gateforge.design import DesignContext
from gateforge.mapping import Mapper, MappingProvider
from gateforge.materialize import flatten_and_materialize
from gateforge.pipeline import MappingStage, default_mapping_stages, run_mapping_stages
from gateforge.providers.lbp.common import LBP_PROVIDER
from gateforge.providers.lbp.mappers import LBPCombinatorialLowLevelGateMapper
from gateforge.providers.lbp.objects import make_lbp_provider
from gateforge.source import DesignSnapshot
from gateforge.state import CompilationIntermediateState
from gateforge.target import PhysicalDesign, TargetProvider


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


def compile_physical(
    path: str,
    *,
    stages: Sequence[MappingStage] | None = None,
    mapping_providers: Sequence[MappingProvider] | None = None,
    target_providers: Mapping[str, TargetProvider] | None = None,
) -> tuple[DesignContext, CompilationIntermediateState, PhysicalDesign]:
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
    state, physical = flatten_and_materialize(
        context,
        state,
        target_providers,
    )

    context.show()
    return context, state, physical


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Compile synthesizable Verilog for a GateForge target."
    )
    parser.add_argument("source", nargs="?", default="scratch/basic.v")
    parser.add_argument(
        "--emit-json",
        type=Path,
        help="Write the final flattened RTLIL design as Yosys JSON.",
    )
    parser.add_argument(
        "--emit-physical",
        type=Path,
        help="Write the materialized physical object/network graph as JSON.",
    )
    parser.add_argument(
        "--emit-state",
        type=Path,
        help="Write semantic prefabs and durable claim definitions as JSON.",
    )
    parser.add_argument(
        "--no-abc",
        action="store_true",
        help="Skip ABC optimization before the final leaf mapping stage.",
    )
    args = parser.parse_args()

    try:
        context, state, physical = compile_physical(
            args.source,
            stages=default_mapping_stages(use_abc=not args.no_abc),
        )
    except RuntimeError as error:
        print(error)
        raise SystemExit(1) from error
    if args.emit_json is not None:
        context.save_json(args.emit_json)
    if args.emit_physical is not None:
        args.emit_physical.write_text(json.dumps(physical.canonical_data(), indent=2))
    if args.emit_state is not None:
        args.emit_state.write_text(json.dumps(state.canonical_data(), indent=2))

    print(
        f"Accepted {len(state.claims)} claims using "
        f"{len(state.prefabs)} semantic prefabs."
    )
    print(
        f"Materialized {len(physical.objects)} objects and "
        f"{len(physical.nets)} networks."
    )


if __name__ == "__main__":
    main()
