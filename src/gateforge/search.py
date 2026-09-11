from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from enum import StrEnum
import hashlib
import json
import math
from typing import Protocol

from gateforge.behavior_source import BehaviorCapture
from gateforge.claims import accept_mapping_proposals
from gateforge.design import DesignCheckpoint, DesignContext
from gateforge.mapping import (
    Mapper,
    MappingProposal,
    ProposalConflictGraph,
    ProposalSetEnumerator,
)
from gateforge.provider import TargetProvider
from gateforge.source import CellIdentifier, DesignSnapshot
from gateforge.state import CompilationIntermediateState


class MappingSearchError(RuntimeError):
    pass


class MappingSearchMode(StrEnum):
    GREEDY = "greedy"
    BEAM = "beam"
    EXHAUSTIVE = "exhaustive"


class SearchCandidateStatus(StrEnum):
    FRONTIER = "frontier"
    PRUNED = "pruned"
    DEDUPLICATED = "deduplicated"


class MappingSearchStage(Protocol):
    @property
    def name(self) -> str: ...

    @property
    def passes(self) -> tuple[str, ...]: ...


@dataclass(frozen=True, slots=True)
class MappingSearchOptions:
    mode: MappingSearchMode = MappingSearchMode.BEAM
    beam_width: int = 16
    stage_alternative_limit: int = 16

    def __post_init__(self) -> None:
        for name in ("beam_width", "stage_alternative_limit"):
            value = getattr(self, name)
            if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
                raise ValueError(f"Mapping search {name} must be a positive integer")


@dataclass(frozen=True, slots=True, order=True)
class CompilationCandidateId:
    value: str


@dataclass(frozen=True, slots=True)
class MappingDecision:
    stage: str
    accepted: tuple[str, ...]
    deferred: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class CompilationCandidate:
    checkpoint: DesignCheckpoint
    state: CompilationIntermediateState
    stage_index: int = 0
    decisions: tuple[MappingDecision, ...] = ()
    lower_bound: float = 0.0
    expected_cost: float = 0.0
    identifier: CompilationCandidateId = field(init=False)

    def __post_init__(self) -> None:
        if self.checkpoint.revision != self.state.revision:
            raise ValueError(
                "Candidate checkpoint and compilation state revisions differ"
            )
        if self.stage_index < 0:
            raise ValueError("Candidate stage index must be nonnegative")
        for name in ("lower_bound", "expected_cost"):
            value = getattr(self, name)
            if not math.isfinite(value) or value < 0:
                raise ValueError(f"Candidate {name} must be finite and nonnegative")
        if self.lower_bound > self.expected_cost:
            raise ValueError("Candidate lower bound cannot exceed expected cost")
        payload = {
            "stage_index": self.stage_index,
            "checkpoint": self.checkpoint.get_digest(),
            "state": self.state.get_digest(),
        }
        encoded = json.dumps(
            payload,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        object.__setattr__(
            self,
            "identifier",
            CompilationCandidateId(hashlib.sha256(encoded).hexdigest()),
        )

    def restore_context(self) -> DesignContext:
        return self.checkpoint.restore()


@dataclass(frozen=True, slots=True)
class MappingSearchStageReport:
    stage: str
    input_candidates: int
    generated_candidates: int
    deduplicated_candidates: int
    pruned_candidates: int
    output_candidates: int

    def canonical_data(self) -> dict[str, object]:
        return {
            "stage": self.stage,
            "input_candidates": self.input_candidates,
            "generated_candidates": self.generated_candidates,
            "deduplicated_candidates": self.deduplicated_candidates,
            "pruned_candidates": self.pruned_candidates,
            "output_candidates": self.output_candidates,
        }


@dataclass(frozen=True, slots=True)
class TerminalCandidateScore:
    candidate: CompilationCandidateId
    material_digest: str
    provider_object_cost: float
    object_count: int
    decisions: tuple[MappingDecision, ...]

    def canonical_data(self) -> dict[str, object]:
        return {
            "candidate": self.candidate.value,
            "material_digest": self.material_digest,
            "score": {
                "provider_object_cost": self.provider_object_cost,
                "object_count": self.object_count,
            },
            "decisions": [
                {
                    "stage": decision.stage,
                    "accepted": list(decision.accepted),
                    "deferred": list(decision.deferred),
                }
                for decision in self.decisions
            ],
        }


@dataclass(frozen=True, slots=True)
class MappingSearchReport:
    stages: tuple[MappingSearchStageReport, ...]
    terminal_scores: tuple[TerminalCandidateScore, ...] = ()
    winner: CompilationCandidateId | None = None

    def canonical_data(self) -> dict[str, object]:
        return {
            "schema_version": 1,
            "stages": [stage.canonical_data() for stage in self.stages],
            "terminal_candidates": [
                score.canonical_data() for score in self.terminal_scores
            ],
            "winner": None if self.winner is None else self.winner.value,
        }


@dataclass(frozen=True, slots=True)
class MappingSearchResult:
    candidates: tuple[CompilationCandidate, ...]
    report: MappingSearchReport
    behavior: BehaviorCapture | None = None


@dataclass(frozen=True, slots=True)
class MappingProposalSummary:
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
    def from_proposal(cls, proposal: MappingProposal) -> "MappingProposalSummary":
        return cls(
            fingerprint=proposal.fingerprint(),
            provider=proposal.provider,
            mapper=proposal.mapper,
            rule=proposal.rule,
            rule_version=proposal.rule_version,
            cells=tuple(
                (identifier.module, identifier.name, identifier.expected_type)
                for identifier in sorted(proposal.ids)
            ),
            disposition=proposal.disposition.value,
            priority=proposal.priority,
            score=proposal.score,
            lower_bound=proposal.cost.lower_bound,
            expected_cost=proposal.cost.expected,
            implementation_name=proposal.implementation_name,
            packaging=proposal.packaging.value,
        )


@dataclass(frozen=True, slots=True)
class MappingCandidateTransition:
    source: CompilationCandidateId
    candidate: CompilationCandidate
    status: SearchCandidateStatus


@dataclass(frozen=True, slots=True)
class MappingCandidateExpansion:
    source: CompilationCandidate
    post_pass_checkpoint: DesignCheckpoint
    proposals: tuple[MappingProposalSummary, ...]
    transitions: tuple[MappingCandidateTransition, ...]


@dataclass(frozen=True, slots=True)
class MappingSearchStageHistory:
    stage: str
    report: MappingSearchStageReport
    expansions: tuple[MappingCandidateExpansion, ...]

    @property
    def transitions(self) -> tuple[MappingCandidateTransition, ...]:
        return tuple(
            transition
            for expansion in self.expansions
            for transition in expansion.transitions
        )


@dataclass(frozen=True, slots=True)
class _ExpandedCandidate:
    source: CompilationCandidate
    post_pass_checkpoint: DesignCheckpoint
    proposals: tuple[MappingProposalSummary, ...]
    candidates: tuple[CompilationCandidate, ...]


def residual_cells(snapshot: DesignSnapshot) -> frozenset[CellIdentifier]:
    module_names = set(snapshot.modules)
    return frozenset(
        cell.identifier
        for module in snapshot.modules.values()
        if "blackbox" not in module.attributes
        for cell in module.cells.values()
        if "gateforge_prefab" not in cell.attributes
        and cell.identifier.expected_type not in module_names
        and cell.identifier.expected_type != "$scopeinfo"
    )


class CompilationSearch:
    def __init__(
        self,
        mapper: Mapper,
        providers: Mapping[str, TargetProvider],
        options: MappingSearchOptions = MappingSearchOptions(),
    ) -> None:
        self.mapper = mapper
        self.providers = providers
        self.options = options

    def run(
        self,
        context: DesignContext,
        state: CompilationIntermediateState,
        stages: Sequence[MappingSearchStage],
    ) -> MappingSearchResult:
        return self.start(context, state, stages).finish()

    def start(
        self,
        context: DesignContext,
        state: CompilationIntermediateState,
        stages: Sequence[MappingSearchStage],
        *,
        behavior: BehaviorCapture | None = None,
    ) -> "CompilationSearchSession":
        return CompilationSearchSession(self, context, state, stages, behavior=behavior)

    def _expand_candidate(
        self,
        candidate: CompilationCandidate,
        stage: MappingSearchStage,
        terminal: bool,
    ) -> _ExpandedCandidate:
        context = candidate.restore_context()
        state = candidate.state
        for command in stage.passes:
            context.run_pass(command)
            state = state.with_revision(context.revision)

        snapshot = context.snapshot()
        proposals = self.mapper.collect_proposals(snapshot, stage.name)
        if self.options.mode == MappingSearchMode.GREEDY:
            alternatives = (tuple(self.mapper.combine(proposals, snapshot.revision)),)
        else:
            required = frozenset(
                (identifier.module, identifier.name)
                for identifier in residual_cells(snapshot)
            ) if terminal else frozenset()
            alternatives = ProposalSetEnumerator(
                ProposalConflictGraph.from_proposals(proposals)
            ).enumerate(
                limit=self.options.stage_alternative_limit,
                required_cells=required,
            )

        if terminal and self.options.mode != MappingSearchMode.GREEDY:
            required = residual_cells(snapshot)
            alternatives = tuple(
                selected
                for selected in alternatives
                if required.issubset(
                    identifier
                    for proposal in selected
                    for identifier in proposal.ids
                )
            )

        post_pass_checkpoint = context.checkpoint()
        proposal_fingerprints = {
            proposal.fingerprint(): proposal for proposal in proposals
        }
        expanded: list[CompilationCandidate] = []
        for selected in alternatives:
            branch = post_pass_checkpoint.restore()
            branch_state = state
            if selected:
                branch_state = accept_mapping_proposals(
                    branch,
                    branch_state,
                    selected,
                    self.providers,
                )
            accepted = tuple(sorted(proposal.fingerprint() for proposal in selected))
            deferred = tuple(
                sorted(set(proposal_fingerprints) - set(accepted))
            )
            expanded.append(
                CompilationCandidate(
                    checkpoint=branch.checkpoint(),
                    state=branch_state,
                    stage_index=candidate.stage_index + 1,
                    decisions=(
                        *candidate.decisions,
                        MappingDecision(stage.name, accepted, deferred),
                    ),
                    lower_bound=candidate.lower_bound
                    + sum(proposal.cost.lower_bound for proposal in selected),
                    expected_cost=candidate.expected_cost
                    + sum(proposal.cost.expected for proposal in selected),
                )
            )
        return _ExpandedCandidate(
            source=candidate,
            post_pass_checkpoint=post_pass_checkpoint,
            proposals=tuple(MappingProposalSummary.from_proposal(item) for item in proposals),
            candidates=tuple(expanded),
        )

    @staticmethod
    def _candidate_order(
        candidate: CompilationCandidate,
    ) -> tuple[float, float, str]:
        return (
            candidate.expected_cost,
            candidate.lower_bound,
            candidate.identifier.value,
        )

    @classmethod
    def _deduplicate(
        cls,
        candidates: Sequence[CompilationCandidate],
    ) -> tuple[CompilationCandidate, ...]:
        unique: dict[CompilationCandidateId, CompilationCandidate] = {}
        for candidate in candidates:
            existing = unique.get(candidate.identifier)
            if existing is None or cls._candidate_order(candidate) < cls._candidate_order(
                existing
            ):
                unique[candidate.identifier] = candidate
        return tuple(unique.values())


class CompilationSearchSession:
    def __init__(
        self,
        search: CompilationSearch,
        context: DesignContext,
        state: CompilationIntermediateState,
        stages: Sequence[MappingSearchStage],
        *,
        behavior: BehaviorCapture | None = None,
    ) -> None:
        if context.revision != state.revision:
            raise ValueError(
                f"State revision {state.revision} does not match design revision "
                f"{context.revision}"
            )
        if behavior is not None and behavior.revision != context.revision:
            raise MappingSearchError("Behavior capture belongs to a different source revision")
        self.behavior = behavior
        self.search = search
        self.stages = tuple(stages)
        self.stage_index = 0
        self.frontier = (
            CompilationCandidate(
                checkpoint=context.checkpoint(),
                state=state,
            ),
        )
        self._reports: list[MappingSearchStageReport] = []
        self._history: list[MappingSearchStageHistory] = []

    @property
    def complete(self) -> bool:
        return self.stage_index == len(self.stages)

    @property
    def reports(self) -> tuple[MappingSearchStageReport, ...]:
        return tuple(self._reports)

    @property
    def history(self) -> tuple[MappingSearchStageHistory, ...]:
        return tuple(self._history)

    def advance_stage(self) -> MappingSearchStageHistory:
        if self.complete:
            raise MappingSearchError("Mapping search has no remaining stages")
        stage = self.stages[self.stage_index]
        expansions = tuple(
            self.search._expand_candidate(
                source,
                stage,
                self.stage_index == len(self.stages) - 1,
            )
            for source in self.frontier
        )
        generated = tuple(
            candidate
            for expansion in expansions
            for candidate in expansion.candidates
        )
        deduplicated = self.search._deduplicate(generated)
        ordered = tuple(sorted(deduplicated, key=self.search._candidate_order))
        if self.search.options.mode == MappingSearchMode.BEAM:
            next_frontier = ordered[: self.search.options.beam_width]
        elif self.search.options.mode == MappingSearchMode.GREEDY:
            next_frontier = ordered[:1]
        else:
            next_frontier = ordered
        report = MappingSearchStageReport(
            stage=stage.name,
            input_candidates=len(self.frontier),
            generated_candidates=len(generated),
            deduplicated_candidates=len(generated) - len(deduplicated),
            pruned_candidates=len(ordered) - len(next_frontier),
            output_candidates=len(next_frontier),
        )
        representative_objects = {id(candidate) for candidate in deduplicated}
        frontier_objects = {id(candidate) for candidate in next_frontier}
        history = MappingSearchStageHistory(
            stage=stage.name,
            report=report,
            expansions=tuple(
                MappingCandidateExpansion(
                    source=expansion.source,
                    post_pass_checkpoint=expansion.post_pass_checkpoint,
                    proposals=expansion.proposals,
                    transitions=tuple(
                        MappingCandidateTransition(
                            source=expansion.source.identifier,
                            candidate=candidate,
                            status=(
                                SearchCandidateStatus.DEDUPLICATED
                                if id(candidate) not in representative_objects
                                else SearchCandidateStatus.FRONTIER
                                if id(candidate) in frontier_objects
                                else SearchCandidateStatus.PRUNED
                            ),
                        )
                        for candidate in expansion.candidates
                    ),
                )
                for expansion in expansions
            ),
        )
        self._reports.append(report)
        self._history.append(history)
        if not next_frontier:
            raise MappingSearchError(
                f"Mapping search produced no candidates at stage {stage.name!r}"
            )
        self.frontier = next_frontier
        self.stage_index += 1
        return history

    def result(self) -> MappingSearchResult:
        if not self.complete:
            raise MappingSearchError("Mapping search has unfinished stages")
        return MappingSearchResult(
            self.frontier,
            MappingSearchReport(tuple(self._reports)),
            self.behavior,
        )

    def finish(self) -> MappingSearchResult:
        while not self.complete:
            self.advance_stage()
        return self.result()