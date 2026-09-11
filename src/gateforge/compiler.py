from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
import json
import os
from pathlib import Path

from pyosys import libyosys as ys

from gateforge.backend import CompilationBackend
from gateforge.behavior_source import (
    BehaviorCapture,
    BehaviorLowerer,
    MaterialBehaviorBoundary,
    YosysCombinationalBehaviorLowerer,
    bind_material_behavior,
)
from gateforge.design import DesignContext
from gateforge.hierarchy import (
    SynthesisHierarchyPolicy,
    apply_synthesis_hierarchy,
)
from gateforge.mapping import Mapper, MappingProvider
from gateforge.material import MaterialDesign
from gateforge.materialize import flatten_and_materialize
from gateforge.pipeline import MappingStage, default_mapping_stages
from gateforge.provider import TargetProvider
from gateforge.providers.lbp.associative import LBPAssociativeConeMapper
from gateforge.providers.lbp.common import LBP_PROVIDER
from gateforge.providers.lbp.intrinsics import LBPIntrinsicMapper
from gateforge.providers.lbp.mappers import LBPCombinatorialLowLevelGateMapper
from gateforge.providers.lbp.objects import make_lbp_provider
from gateforge.providers.lbp.registers import (
    LBPCoarseRegisterBankMapper,
    LBPRegisterStyle,
    LBPScalarRegisterBankMapper,
)
from gateforge.providers.factorio.common import FACTORIO_PROVIDER
from gateforge.providers.factorio.mapping import FactorioAddMapper
from gateforge.providers.factorio.intrinsics import FactorioLampIntrinsicMapper
from gateforge.providers.factorio.objects import make_factorio_provider
from gateforge.placement import TopologicalPlacementOptions
from gateforge.realization import (
    DirectMaterialRealizationStrategy,
    RealizationError,
    RealizationInfeasibleError,
    RealizationOptions,
    RealizationOutcome,
)
from gateforge.search import (
    CompilationSearch,
    CompilationSearchSession,
    MappingSearchMode,
    MappingSearchOptions,
    MappingSearchReport,
    MappingSearchResult,
    TerminalCandidateScore,
)
from gateforge.source import DesignSnapshot
from gateforge.state import CompilationIntermediateState


@dataclass(frozen=True, slots=True)
class MaterialCompilationResult:
    context: DesignContext
    state: CompilationIntermediateState
    material: MaterialDesign
    report: MappingSearchReport


@dataclass(frozen=True, slots=True)
class MaterialCompilationCandidate:
    context: DesignContext
    state: CompilationIntermediateState
    material: MaterialDesign
    score: TerminalCandidateScore
    behavior: BehaviorCapture | None = None
    behavior_boundary: MaterialBehaviorBoundary | None = None

    @property
    def baseline_order(self) -> tuple[float, int, str, str]:
        return (
            self.score.provider_object_cost,
            self.score.object_count,
            self.score.material_digest,
            self.score.candidate.value,
        )


@dataclass(frozen=True, slots=True)
class MaterialCompilationCandidates:
    candidates: tuple[MaterialCompilationCandidate, ...]
    report: MappingSearchReport


@dataclass(frozen=True, slots=True)
class RealizedCompilationCandidate:
    baseline: MaterialCompilationCandidate
    outcome: RealizationOutcome

    @property
    def selection_order(self) -> tuple[float, tuple[float, int, str, str], str]:
        return (
            self.outcome.score.total,
            self.baseline.baseline_order,
            self.outcome.artifact_digest(),
        )


@dataclass(frozen=True, slots=True)
class RealizationRejection:
    candidate: str
    reason: str


@dataclass(frozen=True, slots=True)
class RealizationCompilationResult:
    materialized: MaterialCompilationCandidates
    candidates: tuple[RealizedCompilationCandidate, ...]
    rejections: tuple[RealizationRejection, ...]

    @property
    def winner(self) -> RealizedCompilationCandidate:
        return self.candidates[0]


def compilation_backend(
    target: str = LBP_PROVIDER,
    register_style: LBPRegisterStyle = LBPRegisterStyle.COMPACT,
) -> CompilationBackend:
    if target == LBP_PROVIDER:
        provider = make_lbp_provider()
        return CompilationBackend(
            identifier=LBP_PROVIDER,
            target_providers={LBP_PROVIDER: provider},
            mapping_providers=(
                LBPIntrinsicMapper(),
                LBPCoarseRegisterBankMapper(register_style),
                LBPScalarRegisterBankMapper(register_style),
                LBPAssociativeConeMapper(),
                LBPCombinatorialLowLevelGateMapper(),
            ),
            placement_options=TopologicalPlacementOptions(),
            realization_strategy=DirectMaterialRealizationStrategy(),
        )
    if target == FACTORIO_PROVIDER:
        from gateforge.providers.factorio.strategy import FactorioRealizationStrategy

        provider = make_factorio_provider()
        realization = FactorioRealizationStrategy()
        return CompilationBackend(
            identifier=FACTORIO_PROVIDER,
            target_providers={FACTORIO_PROVIDER: provider},
            mapping_providers=(FactorioLampIntrinsicMapper(), FactorioAddMapper()),
            realization_strategy=realization,
            realization_presenter=realization,
            behavior_lowerer=YosysCombinationalBehaviorLowerer(
                observation_ports=(("GF_Lamp", "in"),)
            ),
            placement_options=TopologicalPlacementOptions(
                column_pitch=6.0,
                row_pitch=3.0,
                routing_group_height=8.0,
                routing_gap_rows=1,
            ),
        )
    raise ValueError(f"Unknown compilation target {target!r}")


def all_target_providers() -> dict[str, TargetProvider]:
    return {
        target: compilation_backend(target).target_providers[target]
        for target in (LBP_PROVIDER, FACTORIO_PROVIDER)
    }


def default_target_providers(
    target: str = LBP_PROVIDER,
) -> dict[str, TargetProvider]:
    return dict(compilation_backend(target).target_providers)


def default_mapping_providers(
    register_style: LBPRegisterStyle = LBPRegisterStyle.COMPACT,
    *,
    target: str = LBP_PROVIDER,
) -> tuple[MappingProvider, ...]:
    return compilation_backend(target, register_style).mapping_providers


def design_preprocess(path: str) -> DesignContext:
    context = DesignContext(ys.Design())
    intrinsic_library = Path(__file__).parent / "intrinsics" / "gateforge_intrinsics.v"
    context.run_pass(
        f"read_verilog -lib {json.dumps(str(intrinsic_library.resolve()))}"
    )
    context.run_pass(f"read_verilog {json.dumps(os.path.abspath(path))}")
    context.run_pass("hierarchy -check -auto-top")
    context.run_pass("proc -noopt")
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


def start_compilation_search(
    path: str,
    *,
    stages: Sequence[MappingStage] | None = None,
    mapping_providers: Sequence[MappingProvider] | None = None,
    target_providers: Mapping[str, TargetProvider] | None = None,
    synthesis_hierarchy: SynthesisHierarchyPolicy = SynthesisHierarchyPolicy(),
    mapping_search: MappingSearchOptions = MappingSearchOptions(
        mode=MappingSearchMode.GREEDY
    ),
    target: str = LBP_PROVIDER,
    behavior_lowerer: BehaviorLowerer | None = None,
) -> CompilationSearchSession:
    resolved_stages = tuple(default_mapping_stages() if stages is None else stages)
    resolved_mapping_providers = (
        default_mapping_providers(target=target)
        if mapping_providers is None
        else tuple(mapping_providers)
    )
    resolved_target_providers = (
        default_target_providers(target)
        if target_providers is None
        else target_providers
    )
    context = design_preprocess(path)
    apply_synthesis_hierarchy(context, synthesis_hierarchy)
    behavior = None if behavior_lowerer is None else behavior_lowerer.lower(context.snapshot())
    state = CompilationIntermediateState.empty(context.revision)
    return CompilationSearch(
        Mapper(resolved_mapping_providers),
        resolved_target_providers,
        mapping_search,
    ).start(context, state, resolved_stages, behavior=behavior)


def search_source(
    path: str,
    stages: Sequence[MappingStage],
    mapping_providers: Sequence[MappingProvider],
    target_providers: Mapping[str, TargetProvider],
    synthesis_hierarchy: SynthesisHierarchyPolicy,
    mapping_search: MappingSearchOptions,
) -> MappingSearchResult:
    return start_compilation_search(
        path,
        stages=stages,
        mapping_providers=mapping_providers,
        target_providers=target_providers,
        synthesis_hierarchy=synthesis_hierarchy,
        mapping_search=mapping_search,
    ).finish()


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
    target: str = LBP_PROVIDER,
) -> tuple[DesignContext, CompilationIntermediateState]:
    resolved_stages = default_mapping_stages() if stages is None else stages
    resolved_mapping_providers = (
        default_mapping_providers(target=target)
        if mapping_providers is None
        else mapping_providers
    )
    resolved_target_providers = (
        default_target_providers(target)
        if target_providers is None
        else target_providers
    )
    result = search_source(
        path,
        resolved_stages,
        resolved_mapping_providers,
        resolved_target_providers,
        synthesis_hierarchy,
        mapping_search,
    )
    winner = result.candidates[0]
    return winner.restore_context(), winner.state


def materialize_search_candidates(
    result: MappingSearchResult,
    target_providers: Mapping[str, TargetProvider],
) -> MaterialCompilationCandidates:
    evaluated: list[MaterialCompilationCandidate] = []
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
            MaterialCompilationCandidate(
                context,
                state,
                material,
                terminal_score,
                result.behavior,
                None if result.behavior is None else bind_material_behavior(material, result.behavior),
            )
        )
    if not evaluated:
        details = ", ".join(sorted(set(residual_details)))
        raise RuntimeError(f"Design contains unmapped cells: {details}")
    report = replace(
        result.report,
        terminal_scores=tuple(
            sorted(
                (item.score for item in evaluated),
                key=lambda item: item.candidate.value,
            )
        ),
        winner=None,
    )
    return MaterialCompilationCandidates(
        tuple(sorted(evaluated, key=lambda item: item.score.candidate.value)),
        report,
    )


def materialize_search_result(
    result: MappingSearchResult,
    target_providers: Mapping[str, TargetProvider],
) -> MaterialCompilationResult:
    materialized = materialize_search_candidates(result, target_providers)
    winner = min(materialized.candidates, key=lambda item: item.baseline_order)
    return MaterialCompilationResult(
        winner.context,
        winner.state,
        winner.material,
        replace(materialized.report, winner=winner.score.candidate),
    )


def realize_search_result(
    result: MappingSearchResult,
    backend: CompilationBackend,
    *,
    options: RealizationOptions | None = None,
) -> RealizationCompilationResult:
    return realize_material_candidates(
        materialize_search_candidates(result, backend.target_providers), backend, options=options
    )


def realize_material_candidates(
    materialized: MaterialCompilationCandidates,
    backend: CompilationBackend,
    *,
    options: RealizationOptions | None = None,
) -> RealizationCompilationResult:
    from gateforge.realization import RealizationProblem

    strategy = backend.realization_strategy
    if strategy is None:
        raise RealizationError(
            f"Backend {backend.identifier!r} has no realization strategy"
        )
    resolved_options = options or RealizationOptions(placement=backend.placement_options)
    candidates: list[RealizedCompilationCandidate] = []
    rejections: list[RealizationRejection] = []
    for baseline in materialized.candidates:
        generated = False
        try:
            problem = RealizationProblem(baseline.material, backend.target_providers, baseline.behavior, baseline.behavior_boundary)
            source_aware = getattr(strategy, "realize_problem", None)
            outcomes = (
                source_aware(problem, resolved_options) if source_aware is not None
                else strategy.realize(baseline.material, backend.target_providers, resolved_options)
            )
            for outcome in outcomes:
                if outcome.artifact.target != backend.identifier:
                    raise RealizationError("Realization artifact targets a different backend")
                outcome.artifact.validate(baseline.material, backend.target_providers)
                outcome.artifact_digest()
                candidates.append(RealizedCompilationCandidate(baseline, outcome))
                generated = True
        except RealizationInfeasibleError as error:
            rejections.append(RealizationRejection(baseline.score.candidate.value, str(error)))
            continue
        if not generated:
            rejections.append(
                RealizationRejection(
                    baseline.score.candidate.value, "Strategy produced no realizations"
                )
            )
    if not candidates:
        details = "; ".join(item.reason for item in rejections)
        raise RealizationInfeasibleError(f"No feasible realizations: {details}")
    return RealizationCompilationResult(
        materialized,
        tuple(sorted(candidates, key=lambda item: item.selection_order)),
        tuple(rejections),
    )


def compile_realization(
    path: str,
    *,
    backend: CompilationBackend | None = None,
    stages: Sequence[MappingStage] | None = None,
    synthesis_hierarchy: SynthesisHierarchyPolicy = SynthesisHierarchyPolicy(),
    mapping_search: MappingSearchOptions = MappingSearchOptions(
        mode=MappingSearchMode.GREEDY
    ),
    options: RealizationOptions | None = None,
) -> RealizationCompilationResult:
    resolved_backend = compilation_backend() if backend is None else backend
    if resolved_backend.realization_strategy is None:
        raise RealizationError(
            f"Backend {resolved_backend.identifier!r} has no realization strategy"
        )
    result = start_compilation_search(
        path,
        stages=stages,
        mapping_providers=resolved_backend.mapping_providers,
        target_providers=resolved_backend.target_providers,
        synthesis_hierarchy=synthesis_hierarchy,
        mapping_search=mapping_search,
        target=resolved_backend.identifier,
        behavior_lowerer=resolved_backend.behavior_lowerer,
    ).finish()
    return realize_search_result(result, resolved_backend, options=options)


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
    target: str = LBP_PROVIDER,
) -> tuple[DesignContext, CompilationIntermediateState, MaterialDesign]:
    compilation = compile_material_with_report(
        path,
        stages=stages,
        mapping_providers=mapping_providers,
        target_providers=target_providers,
        synthesis_hierarchy=synthesis_hierarchy,
        mapping_search=mapping_search,
        target=target,
    )
    return compilation[0], compilation[1], compilation[2]


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
    target: str = LBP_PROVIDER,
) -> tuple[
    DesignContext,
    CompilationIntermediateState,
    MaterialDesign,
    MappingSearchReport,
]:
    resolved_stages = default_mapping_stages() if stages is None else stages
    resolved_mapping_providers = (
        default_mapping_providers(target=target)
        if mapping_providers is None
        else mapping_providers
    )
    resolved_target_providers = (
        default_target_providers(target)
        if target_providers is None
        else target_providers
    )
    result = search_source(
        path,
        resolved_stages,
        resolved_mapping_providers,
        resolved_target_providers,
        synthesis_hierarchy,
        mapping_search,
    )
    compilation = materialize_search_result(result, resolved_target_providers)
    return (
        compilation.context,
        compilation.state,
        compilation.material,
        compilation.report,
    )