from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
import hashlib
import json
from pathlib import Path

from gateforge.artifacts import write_json
from gateforge.compiler import (
    MaterialCompilationResult,
    compilation_backend,
    default_mapping_providers,
    default_target_providers,
    materialize_search_result,
    start_compilation_search,
)
from gateforge.design import DesignCheckpoint
from gateforge.graph import MaterialGraph
from gateforge.hierarchy import (
    GeneratedHierarchyPolicy,
    PhysicalHierarchyPolicy,
    SynthesisHierarchyPolicy,
)
from gateforge.mapping import MappingProvider
from gateforge.material import MaterialDesign
from gateforge.placement import (
    PlacedDesign,
    Placer,
    TopologicalPlacementOptions,
    TopologicalPlacer,
)
from gateforge.pipeline import MappingStage, default_mapping_stages
from gateforge.provider import TargetProvider
from gateforge.search import (
    CompilationCandidate,
    CompilationCandidateId,
    CompilationSearchSession,
    MappingCandidateTransition,
    MappingDecision,
    MappingProposalSummary,
    MappingSearchMode,
    MappingSearchOptions,
    MappingSearchResult,
    MappingSearchStageHistory,
    SearchCandidateStatus,
)
from gateforge.state import ClaimDefinition
from gateforge.visualization.build import (
    build_visual_document as build_provider_visual_document,
)
from gateforge.visualization.model import VisualDocument


class CompilationSessionError(RuntimeError):
    pass


class CompilationSessionPhase(StrEnum):
    SOURCE_LOADED = "source-loaded"
    PREPROCESSED = "preprocessed"
    MAPPING = "mapping"
    SEARCH_COMPLETE = "search-complete"
    MATERIALIZED = "materialized"
    PLACED = "placed"
    REALIZED = "realized"


@dataclass(frozen=True, slots=True)
class CandidateSummary:
    identifier: str
    stage_index: int
    checkpoint_digest: str
    revision: int
    lower_bound: float
    expected_cost: float
    claim_count: int
    decision_count: int

    @classmethod
    def from_candidate(cls, candidate: CompilationCandidate) -> "CandidateSummary":
        return cls(
            identifier=candidate.identifier.value,
            stage_index=candidate.stage_index,
            checkpoint_digest=candidate.checkpoint.get_digest(),
            revision=candidate.checkpoint.revision,
            lower_bound=candidate.lower_bound,
            expected_cost=candidate.expected_cost,
            claim_count=len(candidate.state.claims),
            decision_count=len(candidate.decisions),
        )

    def canonical_data(self) -> dict[str, object]:
        return {
            "identifier": self.identifier,
            "stage_index": self.stage_index,
            "checkpoint_digest": self.checkpoint_digest,
            "revision": self.revision,
            "lower_bound": self.lower_bound,
            "expected_cost": self.expected_cost,
            "claim_count": self.claim_count,
            "decision_count": self.decision_count,
        }


@dataclass(frozen=True, slots=True)
class ProposalSummary:
    source_candidate: str
    fingerprint: str
    provider: str
    mapper: str
    rule: str
    rule_version: int
    cells: tuple[tuple[str, str, str], ...]
    disposition: str
    priority: int
    score: int
    lower_bound: float
    expected_cost: float
    implementation_name: str
    packaging: str

    @classmethod
    def from_search_summary(
        cls,
        source: CompilationCandidateId,
        proposal: MappingProposalSummary,
    ) -> "ProposalSummary":
        return cls(
            source_candidate=source.value,
            fingerprint=proposal.fingerprint,
            provider=proposal.provider,
            mapper=proposal.mapper,
            rule=proposal.rule,
            rule_version=proposal.rule_version,
            cells=proposal.cells,
            disposition=proposal.disposition,
            priority=proposal.priority,
            score=proposal.score,
            lower_bound=proposal.lower_bound,
            expected_cost=proposal.expected_cost,
            implementation_name=proposal.implementation_name,
            packaging=proposal.packaging,
        )

    def canonical_data(self) -> dict[str, object]:
        return {
            "source_candidate": self.source_candidate,
            "fingerprint": self.fingerprint,
            "provider": self.provider,
            "mapper": self.mapper,
            "rule": self.rule,
            "rule_version": self.rule_version,
            "cells": [list(item) for item in self.cells],
            "disposition": self.disposition,
            "priority": self.priority,
            "score": self.score,
            "cost": {
                "lower_bound": self.lower_bound,
                "expected": self.expected_cost,
            },
            "implementation_name": self.implementation_name,
            "packaging": self.packaging,
        }


@dataclass(frozen=True, slots=True)
class CandidateTransitionSummary:
    source_candidate: str
    candidate: CandidateSummary
    status: str
    accepted: tuple[str, ...]
    deferred: tuple[str, ...]

    @classmethod
    def from_transition(
        cls,
        transition: MappingCandidateTransition,
    ) -> "CandidateTransitionSummary":
        decision = transition.candidate.decisions[-1]
        return cls(
            source_candidate=transition.source.value,
            candidate=CandidateSummary.from_candidate(transition.candidate),
            status=transition.status.value,
            accepted=decision.accepted,
            deferred=decision.deferred,
        )

    def canonical_data(self) -> dict[str, object]:
        return {
            "source_candidate": self.source_candidate,
            "candidate": self.candidate.canonical_data(),
            "status": self.status,
            "accepted": list(self.accepted),
            "deferred": list(self.deferred),
        }


@dataclass(frozen=True, slots=True)
class StageSummary:
    name: str
    input_candidates: int
    generated_candidates: int
    deduplicated_candidates: int
    pruned_candidates: int
    output_candidates: int
    post_pass_checkpoints: tuple[tuple[str, str], ...]
    proposals: tuple[ProposalSummary, ...]
    transitions: tuple[CandidateTransitionSummary, ...]

    @classmethod
    def from_history(cls, history: MappingSearchStageHistory) -> "StageSummary":
        return cls(
            name=history.stage,
            input_candidates=history.report.input_candidates,
            generated_candidates=history.report.generated_candidates,
            deduplicated_candidates=history.report.deduplicated_candidates,
            pruned_candidates=history.report.pruned_candidates,
            output_candidates=history.report.output_candidates,
            post_pass_checkpoints=tuple(
                (
                    expansion.source.identifier.value,
                    expansion.post_pass_checkpoint.get_digest(),
                )
                for expansion in history.expansions
            ),
            proposals=tuple(
                ProposalSummary.from_search_summary(
                    expansion.source.identifier,
                    proposal,
                )
                for expansion in history.expansions
                for proposal in expansion.proposals
            ),
            transitions=tuple(
                CandidateTransitionSummary.from_transition(transition)
                for transition in history.transitions
            ),
        )

    def canonical_data(self) -> dict[str, object]:
        return {
            "name": self.name,
            "input_candidates": self.input_candidates,
            "generated_candidates": self.generated_candidates,
            "deduplicated_candidates": self.deduplicated_candidates,
            "pruned_candidates": self.pruned_candidates,
            "output_candidates": self.output_candidates,
            "post_pass_checkpoints": [
                {"candidate": candidate, "checkpoint": checkpoint}
                for candidate, checkpoint in self.post_pass_checkpoints
            ],
            "proposals": [item.canonical_data() for item in self.proposals],
            "transitions": [item.canonical_data() for item in self.transitions],
        }


@dataclass(frozen=True, slots=True)
class ClaimSummary:
    identifier: str
    prefab: str
    module: str
    instance: str
    blackbox: str
    provider: str
    mapper: str
    rule: str
    rule_version: int
    accepted_revision: int
    source_provenance: tuple[str, ...]
    implementation_name: str
    packaging: str

    @classmethod
    def from_claim(cls, claim: ClaimDefinition) -> "ClaimSummary":
        return cls(
            identifier=claim.identifier.value,
            prefab=claim.prefab.value,
            module=claim.module,
            instance=claim.instance,
            blackbox=claim.blackbox,
            provider=claim.provider,
            mapper=claim.mapper,
            rule=claim.rule,
            rule_version=claim.rule_version,
            accepted_revision=claim.accepted_revision,
            source_provenance=claim.source_provenance,
            implementation_name=claim.implementation_name,
            packaging=claim.packaging.value,
        )

    def canonical_data(self) -> dict[str, object]:
        return {
            "identifier": self.identifier,
            "prefab": self.prefab,
            "module": self.module,
            "instance": self.instance,
            "blackbox": self.blackbox,
            "provider": self.provider,
            "mapper": self.mapper,
            "rule": self.rule,
            "rule_version": self.rule_version,
            "accepted_revision": self.accepted_revision,
            "source_provenance": list(self.source_provenance),
            "implementation_name": self.implementation_name,
            "packaging": self.packaging,
        }


@dataclass(frozen=True, slots=True)
class DecisionSummary:
    stage: str
    accepted: tuple[str, ...]
    deferred: tuple[str, ...]

    @classmethod
    def from_decision(cls, decision: MappingDecision) -> "DecisionSummary":
        return cls(decision.stage, decision.accepted, decision.deferred)

    def canonical_data(self) -> dict[str, object]:
        return {
            "stage": self.stage,
            "accepted": list(self.accepted),
            "deferred": list(self.deferred),
        }


@dataclass(frozen=True, slots=True)
class CandidateDetails:
    candidate: CandidateSummary
    state_digest: str
    decisions: tuple[DecisionSummary, ...]
    claims: tuple[ClaimSummary, ...]

    def canonical_data(self) -> dict[str, object]:
        return {
            "candidate": self.candidate.canonical_data(),
            "state_digest": self.state_digest,
            "decisions": [item.canonical_data() for item in self.decisions],
            "claims": [item.canonical_data() for item in self.claims],
        }


@dataclass(frozen=True, slots=True)
class ArtifactSummary:
    kind: str
    digest: str | None
    details: tuple[tuple[str, int | float | str], ...]

    def canonical_data(self) -> dict[str, object]:
        return {
            "kind": self.kind,
            "digest": self.digest,
            "details": {name: value for name, value in self.details},
        }


@dataclass(frozen=True, slots=True)
class TerminalScoreSummary:
    candidate: str
    material_digest: str
    provider_object_cost: float
    object_count: int

    def canonical_data(self) -> dict[str, object]:
        return {
            "candidate": self.candidate,
            "material_digest": self.material_digest,
            "provider_object_cost": self.provider_object_cost,
            "object_count": self.object_count,
        }


@dataclass(frozen=True, slots=True)
class SessionSnapshot:
    target: str
    placement_defaults: TopologicalPlacementOptions
    phase: CompilationSessionPhase
    source: str
    stage_index: int
    stage_count: int
    next_stage: str | None
    frontier: tuple[CandidateSummary, ...]
    stages: tuple[StageSummary, ...]
    terminal_scores: tuple[TerminalScoreSummary, ...]
    winner: str | None
    material: ArtifactSummary | None
    placement: ArtifactSummary | None
    visualization: ArtifactSummary | None

    def canonical_data(self) -> dict[str, object]:
        return {
            "schema_version": 2,
            "target": self.target,
            "placement_defaults": {
                "column_pitch": self.placement_defaults.column_pitch,
                "row_pitch": self.placement_defaults.row_pitch,
                "routing_group_height": self.placement_defaults.routing_group_height,
                "routing_gap_rows": self.placement_defaults.routing_gap_rows,
            },
            "phase": self.phase.value,
            "source": self.source,
            "stage_index": self.stage_index,
            "stage_count": self.stage_count,
            "next_stage": self.next_stage,
            "frontier": [item.canonical_data() for item in self.frontier],
            "stages": [item.canonical_data() for item in self.stages],
            "terminal_scores": [
                item.canonical_data() for item in self.terminal_scores
            ],
            "winner": self.winner,
            "artifacts": {
                "material": (
                    None if self.material is None else self.material.canonical_data()
                ),
                "placement": (
                    None if self.placement is None else self.placement.canonical_data()
                ),
                "visualization": (
                    None
                    if self.visualization is None
                    else self.visualization.canonical_data()
                ),
            },
        }


class CompilationSession:
    def __init__(
        self,
        source: str | Path,
        *,
        stages: Sequence[MappingStage] | None = None,
        mapping_providers: Sequence[MappingProvider] | None = None,
        target_providers: Mapping[str, TargetProvider] | None = None,
        synthesis_hierarchy: SynthesisHierarchyPolicy = SynthesisHierarchyPolicy(),
        mapping_search: MappingSearchOptions = MappingSearchOptions(
            mode=MappingSearchMode.GREEDY
        ),
        target: str = "lbp",
    ) -> None:
        self.source = Path(source).resolve()
        self.source_text = self.source.read_text(encoding="utf-8")
        self.stages = tuple(default_mapping_stages() if stages is None else stages)
        backend = compilation_backend(target)
        self.target = backend.identifier
        self.mapping_providers = tuple(
            backend.mapping_providers
            if mapping_providers is None
            else mapping_providers
        )
        self.target_providers = dict(
            backend.target_providers
            if target_providers is None
            else target_providers
        )
        self.default_placement_options = backend.placement_options
        self.synthesis_hierarchy = synthesis_hierarchy
        self.mapping_search = mapping_search
        self.phase = CompilationSessionPhase.SOURCE_LOADED
        self._search: CompilationSearchSession | None = None
        self._search_result: MappingSearchResult | None = None
        self._compilation: MaterialCompilationResult | None = None
        self._graph: MaterialGraph | None = None
        self._placement: PlacedDesign | None = None
        self._visualization: VisualDocument | None = None
        self._stage_summaries: list[StageSummary] = []
        self._candidates: dict[str, CompilationCandidate] = {}
        self._checkpoints: dict[str, DesignCheckpoint] = {}

    @property
    def material(self) -> MaterialDesign | None:
        return None if self._compilation is None else self._compilation.material

    @property
    def placement(self) -> PlacedDesign | None:
        return self._placement

    @property
    def visual_document(self) -> VisualDocument | None:
        return self._visualization

    @property
    def search_result(self) -> MappingSearchResult | None:
        return self._search_result

    @property
    def compilation_result(self) -> MaterialCompilationResult | None:
        return self._compilation

    def preprocess(self) -> SessionSnapshot:
        self._require_phase(CompilationSessionPhase.SOURCE_LOADED)
        self._search = start_compilation_search(
            str(self.source),
            stages=self.stages,
            mapping_providers=self.mapping_providers,
            target_providers=self.target_providers,
            synthesis_hierarchy=self.synthesis_hierarchy,
            mapping_search=self.mapping_search,
        )
        for candidate in self._search.frontier:
            self._index_candidate(candidate)
        if self._search.complete:
            self._search_result = self._search.result()
            self.phase = CompilationSessionPhase.SEARCH_COMPLETE
        else:
            self.phase = CompilationSessionPhase.PREPROCESSED
        return self.snapshot()

    def advance_stage(self) -> StageSummary:
        self._require_phase(
            CompilationSessionPhase.PREPROCESSED,
            CompilationSessionPhase.MAPPING,
        )
        search = self._require_search()
        history = search.advance_stage()
        for expansion in history.expansions:
            self._index_candidate(expansion.source)
            self._index_checkpoint(expansion.post_pass_checkpoint)
            for transition in (
                item
                for item in expansion.transitions
                if item.status != SearchCandidateStatus.DEDUPLICATED
            ):
                self._index_candidate(transition.candidate)
            for transition in (
                item
                for item in expansion.transitions
                if item.status == SearchCandidateStatus.DEDUPLICATED
            ):
                self._index_candidate(transition.candidate, replace=False)
        summary = StageSummary.from_history(history)
        self._stage_summaries.append(summary)
        if search.complete:
            self._search_result = search.result()
            self.phase = CompilationSessionPhase.SEARCH_COMPLETE
        else:
            self.phase = CompilationSessionPhase.MAPPING
        return summary

    def finish_search(self) -> MappingSearchResult:
        if self.phase == CompilationSessionPhase.SOURCE_LOADED:
            self.preprocess()
        while self.phase in {
            CompilationSessionPhase.PREPROCESSED,
            CompilationSessionPhase.MAPPING,
        }:
            self.advance_stage()
        self._require_phase(CompilationSessionPhase.SEARCH_COMPLETE)
        if self._search_result is None:
            raise AssertionError("Complete compilation session has no search result")
        return self._search_result

    def materialize(self) -> MaterialDesign:
        self._require_phase(CompilationSessionPhase.SEARCH_COMPLETE)
        if self._search_result is None:
            raise AssertionError("Complete compilation session has no search result")
        self._compilation = materialize_search_result(
            self._search_result,
            self.target_providers,
        )
        self._graph = MaterialGraph.from_design(
            self._compilation.material,
            self.target_providers,
        )
        self.phase = CompilationSessionPhase.MATERIALIZED
        return self._compilation.material

    def run_to_material(self) -> MaterialDesign:
        self.finish_search()
        return self.materialize()

    def place(
        self,
        *,
        options: TopologicalPlacementOptions | None = None,
        physical_hierarchy: PhysicalHierarchyPolicy = PhysicalHierarchyPolicy(),
        generated_hierarchy: GeneratedHierarchyPolicy = GeneratedHierarchyPolicy(),
        placer: Placer | None = None,
    ) -> PlacedDesign:
        self._require_phase(CompilationSessionPhase.MATERIALIZED)
        if self._graph is None:
            raise AssertionError("Materialized compilation session has no graph")
        resolved_placer = placer or TopologicalPlacer(
            self.default_placement_options if options is None else options,
            providers=self.target_providers,
            physical_hierarchy=physical_hierarchy,
            generated_hierarchy=generated_hierarchy,
        )
        self._placement = resolved_placer.place(self._graph).finalize(self._graph)
        self.phase = CompilationSessionPhase.PLACED
        return self._placement

    def build_visual_document(
        self,
        *,
        provider_options: Mapping[str, Mapping[str, object]] | None = None,
    ) -> VisualDocument:
        self._require_phase(
            CompilationSessionPhase.PLACED,
            CompilationSessionPhase.REALIZED,
        )
        if self._compilation is None or self._graph is None or self._placement is None:
            raise AssertionError("Placed compilation session is missing artifacts")
        self._visualization = build_provider_visual_document(
            self._compilation.material,
            self._graph,
            self._placement,
            self.target_providers,
            provider_options=provider_options,
        )
        self.phase = CompilationSessionPhase.REALIZED
        return self._visualization

    def save_material(self, output_path: str | Path) -> Path:
        material = self.material
        if material is None:
            raise CompilationSessionError("Material has not been compiled")
        return write_json(output_path, material.canonical_data())

    def save_state(self, output_path: str | Path) -> Path:
        if self._compilation is None:
            raise CompilationSessionError("Material has not been compiled")
        return write_json(output_path, self._compilation.state.canonical_data())

    def save_report(self, output_path: str | Path) -> Path:
        if self._compilation is not None:
            report = self._compilation.report
        elif self._search_result is not None:
            report = self._search_result.report
        else:
            raise CompilationSessionError("Mapping search has not completed")
        return write_json(output_path, report.canonical_data())

    def save_placement(self, output_path: str | Path) -> Path:
        if self._placement is None:
            raise CompilationSessionError("Placement has not been generated")
        return write_json(output_path, self._placement.canonical_data())

    def save_visual_document(self, output_path: str | Path) -> Path:
        if self._visualization is None:
            raise CompilationSessionError("Visualization has not been generated")
        return write_json(output_path, self._visualization.canonical_data())

    def export(
        self,
        output_path: str | Path,
        builder: Callable[
            [
                MaterialDesign,
                MaterialGraph,
                PlacedDesign,
                Mapping[str, TargetProvider],
            ],
            object,
        ],
    ) -> Path:
        if self._compilation is None or self._graph is None or self._placement is None:
            raise CompilationSessionError(
                "Material and placement must be generated before export"
            )
        return write_json(
            output_path,
            builder(
                self._compilation.material,
                self._graph,
                self._placement,
                self.target_providers,
            ),
        )

    def candidate_details(
        self,
        identifier: str | CompilationCandidateId,
    ) -> CandidateDetails:
        key = identifier.value if isinstance(identifier, CompilationCandidateId) else identifier
        try:
            candidate = self._candidates[key]
        except KeyError as error:
            raise CompilationSessionError(f"Unknown compilation candidate {key!r}") from error
        return CandidateDetails(
            candidate=CandidateSummary.from_candidate(candidate),
            state_digest=candidate.state.get_digest(),
            decisions=tuple(
                DecisionSummary.from_decision(item) for item in candidate.decisions
            ),
            claims=tuple(
                ClaimSummary.from_claim(claim)
                for claim in sorted(
                    candidate.state.claims.values(),
                    key=lambda item: item.identifier.value,
                )
            ),
        )

    def checkpoint(self, digest: str) -> DesignCheckpoint:
        try:
            return self._checkpoints[digest]
        except KeyError as error:
            raise CompilationSessionError(f"Unknown design checkpoint {digest!r}") from error

    def snapshot(self) -> SessionSnapshot:
        search = self._search
        stage_index = 0 if search is None else search.stage_index
        next_stage = (
            self.stages[stage_index].name if stage_index < len(self.stages) else None
        )
        frontier = () if search is None else tuple(
            CandidateSummary.from_candidate(item) for item in search.frontier
        )
        report = None if self._compilation is None else self._compilation.report
        return SessionSnapshot(
            target=self.target,
            placement_defaults=self.default_placement_options,
            phase=self.phase,
            source=str(self.source),
            stage_index=stage_index,
            stage_count=len(self.stages),
            next_stage=next_stage,
            frontier=frontier,
            stages=tuple(self._stage_summaries),
            terminal_scores=(
                ()
                if report is None
                else tuple(
                    TerminalScoreSummary(
                        candidate=item.candidate.value,
                        material_digest=item.material_digest,
                        provider_object_cost=item.provider_object_cost,
                        object_count=item.object_count,
                    )
                    for item in report.terminal_scores
                )
            ),
            winner=(
                None
                if report is None or report.winner is None
                else report.winner.value
            ),
            material=self._material_summary(),
            placement=self._placement_summary(),
            visualization=self._visualization_summary(),
        )

    def _index_candidate(
        self,
        candidate: CompilationCandidate,
        *,
        replace: bool = True,
    ) -> None:
        if replace:
            self._candidates[candidate.identifier.value] = candidate
        else:
            self._candidates.setdefault(candidate.identifier.value, candidate)
        self._index_checkpoint(candidate.checkpoint)

    def _index_checkpoint(self, checkpoint: DesignCheckpoint) -> None:
        self._checkpoints[checkpoint.get_digest()] = checkpoint

    def _require_search(self) -> CompilationSearchSession:
        if self._search is None:
            raise AssertionError("Preprocessed compilation session has no search")
        return self._search

    def _require_phase(self, *phases: CompilationSessionPhase) -> None:
        if self.phase not in phases:
            expected = ", ".join(item.value for item in phases)
            raise CompilationSessionError(
                f"Compilation session is {self.phase.value}; expected {expected}"
            )

    def _material_summary(self) -> ArtifactSummary | None:
        material = self.material
        if material is None:
            return None
        return ArtifactSummary(
            "material",
            material.get_digest().value,
            (
                ("objects", len(material.objects)),
                ("nets", len(material.nets)),
                ("modules", len(material.modules)),
            ),
        )

    def _placement_summary(self) -> ArtifactSummary | None:
        if self._placement is None:
            return None
        return ArtifactSummary(
            "placement",
            _canonical_digest(self._placement.canonical_data()),
            (
                ("containers", len(self._placement.containers)),
                ("components", len(self._placement.components)),
                ("annotations", len(self._placement.annotations)),
            ),
        )

    def _visualization_summary(self) -> ArtifactSummary | None:
        if self._visualization is None:
            return None
        return ArtifactSummary(
            "visualization",
            None,
            (
                ("views", len(self._visualization.views)),
                (
                    "scenes",
                    sum(len(view.scenes) for view in self._visualization.views),
                ),
                (
                    "elements",
                    sum(
                        len(scene.elements)
                        for view in self._visualization.views
                        for scene in view.scenes
                    ),
                ),
            ),
        )


def _canonical_digest(value: object) -> str:
    encoded = json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()